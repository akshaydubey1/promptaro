# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Git isolation for agents. Author: Akshay Dubey

Each agent works in its own git worktree on its own branch, so several agents
can change the same repo at once without touching your working copy. You
review the diff, then commit, open a PR, merge, or throw it away.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from .tools import _clean_env

HOME = Path(os.environ.get("PROMPTARO_HOME") or os.environ.get("TILLWRIGHT_HOME")
            or os.environ.get("ROUTER_HOME") or Path.home() / ".promptaro")


# Never shown in reviews or committed, even in repos without a .gitignore.
JUNK = [":(exclude,glob)**/" + p for p in (
    "__pycache__/**", "*.pyc", ".pytest_cache/**", ".mypy_cache/**", ".ruff_cache/**", "node_modules/**",
    ".DS_Store", ".venv/**", "venv/**", ".terraform/**", "*.tfstate", "*.tfstate.backup", "dist/**",
    "build/**", ".next/**", "coverage/**", ".coverage", "target/debug/**")]


class GitError(Exception):
    pass


async def git(*args: str, cwd: str | Path, check: bool = True, timeout: float = 120,
              keep_env: tuple = ()) -> str:
    # Scrubbed env: keys in the shell can't be exfiltrated through a repo's hooks, and git
    # can't be prompted into a credential flow (GIT_TERMINAL_PROMPT=0). A hostile repo can
    # point core.hooksPath at scripts inside itself, so hooks are switched off for every call.
    env = _clean_env()
    for k in keep_env:
        if k in os.environ:
            env[k] = os.environ[k]
    try:
        proc = await asyncio.create_subprocess_exec("git", "-c", f"core.hooksPath={os.devnull}", *args, cwd=str(cwd),
                                                    env=env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise GitError("git isn't installed or isn't on PATH. Install git, then try again.")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {args[0]} timed out after {timeout}s and was stopped")
    if check and proc.returncode != 0:
        raise GitError((err or out).decode(errors="replace").strip() or f"git {args[0]} failed")
    return out.decode(errors="replace")


def slug(text: str, n: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].rstrip("-") or "task")


async def repo_root(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise GitError(f"{path} is not a folder")
    try:
        return Path((await git("rev-parse", "--show-toplevel", cwd=p)).strip())
    except GitError:
        raise GitError("not_a_repo")


async def init_repo(path: str) -> Path:
    p = Path(path).expanduser()
    await git("init", cwd=p)
    await git("add", "-A", cwd=p)
    await _commit(p, "Initial commit", allow_empty=True)
    return p


async def _identity_args(cwd: Path) -> list[str]:
    name = (await git("config", "user.name", cwd=cwd, check=False)).strip()
    email = (await git("config", "user.email", cwd=cwd, check=False)).strip()
    args = []
    if not name:
        args += ["-c", "user.name=Promptaro"]
    if not email:
        args += ["-c", "user.email=promptaro@localhost"]
    return args


async def _commit(cwd: Path, message: str, allow_empty: bool = False) -> str:
    ident = await _identity_args(cwd)
    extra = ["--allow-empty"] if allow_empty else []
    await git(*ident, "commit", "-q", "-m", message, *extra, cwd=cwd)
    return (await git("rev-parse", "--short", "HEAD", cwd=cwd)).strip()


async def create_worktree(repo: Path, session_id: str, title: str) -> dict:
    base_branch = (await git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)).strip()
    if base_branch == "HEAD":
        base_branch = ""
    try:
        base_sha = (await git("rev-parse", "HEAD", cwd=repo)).strip()
    except GitError:
        raise GitError("This repository has no commits yet. Make a first commit, then try again.")
    dirty = bool((await git("status", "--porcelain", cwd=repo)).strip())
    branch = f"agent/{slug(title)}-{session_id[:6]}"
    wt = HOME / "worktrees" / f"{repo.name}-{session_id[:8]}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    await git("worktree", "add", "-q", "-b", branch, str(wt), base_sha, cwd=repo)
    return {"worktree": str(wt), "branch": branch, "base_branch": base_branch, "base_sha": base_sha,
            "repo_dirty": dirty}


async def diff(wt: str, base_sha: str) -> dict:
    """Everything the agent changed relative to where it started (committed or not)."""
    await git("add", "-A", "--intent-to-add", "--", ".", *JUNK, cwd=wt, check=False)
    patch = await git("diff", "--no-color", "-M", base_sha, "--", ".", *JUNK, cwd=wt)
    stat = await git("diff", "--numstat", "-M", base_sha, "--", ".", *JUNK, cwd=wt)
    files = []
    for line in stat.splitlines():
        a, d, name = (line.split("\t", 2) + ["", "", ""])[:3]
        files.append({"path": name, "added": int(a) if a.isdigit() else 0, "removed": int(d) if d.isdigit() else 0})
    return {"files": files, "patch": patch[:400_000], "truncated": len(patch) > 400_000}


async def commit(wt: str, message: str) -> str:
    await git("add", "-A", "--", ".", *JUNK, cwd=wt)
    if not (await git("diff", "--cached", "--name-only", cwd=wt)).strip():
        return (await git("rev-parse", "--short", "HEAD", cwd=wt)).strip()
    return await _commit(Path(wt), message)


async def merge_into_base(repo: str, branch: str, base_branch: str) -> str:
    if not base_branch:
        raise GitError("Your repo was on a detached HEAD when the task started; merge manually.")
    current = (await git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)).strip()
    if current != base_branch:
        raise GitError(f"Your repo is on '{current}', not '{base_branch}'. Switch back first.")
    if (await git("status", "--porcelain", "--untracked-files=no", cwd=repo)).strip():
        raise GitError("Your working copy has uncommitted changes. Commit or stash them first.")
    ident = await _identity_args(Path(repo))
    await git(*ident, "merge", "--no-ff", "-q", "-m", f"Merge {branch}", branch, cwd=repo)
    return (await git("rev-parse", "--short", "HEAD", cwd=repo)).strip()


async def open_pr(wt: str, branch: str, title: str, body: str) -> str:
    if not shutil.which("gh"):
        raise GitError("GitHub CLI (gh) isn't installed. Install it with `brew install gh`, then `gh auth login`.")
    remote = (await git("remote", cwd=wt, check=False)).split()
    if not remote:
        raise GitError("This repo has no remote to push to.")
    # SSH_AUTH_SOCK only for the push: ssh remotes need the agent, and hooks are already off.
    await git("push", "-u", remote[0], branch, cwd=wt, timeout=300, keep_env=("SSH_AUTH_SOCK",))
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LANG", "GH_TOKEN", "GITHUB_TOKEN",
                                     "SSH_AUTH_SOCK", "XDG_CONFIG_HOME") if k in os.environ}
    proc = await asyncio.create_subprocess_exec("gh", "pr", "create", "--title", title, "--body", body,
                                                "--head", branch, cwd=wt, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise GitError(err.decode(errors="replace").strip())
    return out.decode().strip().splitlines()[-1]


async def discard(repo: str, wt: str, branch: str) -> None:
    await git("worktree", "remove", "--force", wt, cwd=repo, check=False)
    shutil.rmtree(wt, ignore_errors=True)
    await git("worktree", "prune", cwd=repo, check=False)
    await git("branch", "-D", branch, cwd=repo, check=False)

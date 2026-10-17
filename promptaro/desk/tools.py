# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Agent tools, confined to one workspace folder. Author: Akshay Dubey

Every path is resolved inside the workspace; anything that escapes (../, an
absolute path elsewhere, a symlink pointing out) is refused. Commands run in
the workspace with secrets stripped from the environment, a timeout, and an
approval policy.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

MAX_OUT = 8000          # chars of tool output returned to the model
READ_LINES = 400        # default lines per read_file

TOOL_DOCS = """\
Tools (reply with exactly ONE JSON object per turn, in a ```json block):

{"thought": "<1-2 sentences: what you're doing and why>", "action": "<tool>", "args": {...}}

- list_files   {"path": ".", "depth": 2}                 directory tree (respects .gitignore)
- read_file    {"path": "...", "start": 1, "end": 400}   numbered lines
- search       {"pattern": "regex", "path": "."}          matching lines with file:line
- write_file   {"path": "...", "content": "..."}         create or fully replace a file
- edit_file    {"path": "...", "old": "...", "new": "..."} replace ONE exact, unique snippet
- run          {"command": "...", "timeout": 120}         shell command in the project root
- finish       {"summary": "...", "tests": "passed|failed|not_run"}  stop and report

Rules: prefer edit_file over write_file for existing files; read before editing;
run the project's tests when they exist; keep thoughts short; never ask the user
questions inside a tool call — use finish to report a blocker.
"""

# Commands that only read or build/test inside the project run without asking.
_SAFE = re.compile(r"""^\s*(
    ls|pwd|cat|head|tail|wc|grep|rg|find|tree|echo|which|env\s+--version|diff|sort|uniq|stat|file|du\s+-\w+|
    git\s+(status|diff|log|show|branch|rev-parse|ls-files|blame)|
    (python3?|uv\s+run\s+python)\s+-m\s+(pytest|unittest|py_compile|mypy|ruff|black\s+--check)|
    pytest|ruff(\s+check)?|mypy|flake8|black\s+--check|python3?\s+--version|node\s+--version|
    (npm|pnpm|yarn|bun)\s+(test|run\s+(test|lint|build|typecheck|check))|npx\s+(tsc|eslint|vitest|jest|prettier\s+--check)|
    go\s+(test|build|vet|fmt)|cargo\s+(test|build|check|clippy|fmt)|make(\s+(test|check|lint|build))?|
    terraform\s+(fmt|validate)|tflint|helm\s+(lint|template)|kubectl\s+(get|describe|diff)|
    docker\s+(ps|images)|shellcheck|hadolint|yamllint
)(\s|$)""", re.X)
_NEVER = re.compile(
    r"(\bsudo\b|\bdoas\b|\bsu\s+-"
    r"|rm\s+(-{1,2}[a-z-]*[rf][a-z-]*\s+)+(-{1,2}\S+\s+)*(/|~|\$\{?HOME\}?|\.{2}(?=[\s/]|$)|\.(?=\s|$))"
    r"|:\(\)\s*\{|\bmkfs|\bdd\b[^|;&]*\bof=/dev|\bshred\b|>\s*/dev/(?!null|zero|tty|stdin|stdout|stderr\b)"
    r"|\bchmod\s+-R\b|\bchown\s+-R\b|\bgit\s+clean\b"
    r"|\bformat\b\s+[a-z]:|\bdel\s+/[fsq]|\brd\s+/s\b|\brmdir\s+/s\b)", re.I)
_SPLIT = re.compile(r"&&|\|\||;|\||&|\n")
_SUBST = re.compile(r"`|\$\(|<\(|>\(")
_SUBST_INNER = re.compile(r"\$\(([^)]*)\)|`([^`]*)`")
_REDIR = re.compile(r"(?:^|[^>&])(?:&>>?|>>?)\s*([^\s;&|><]+)")  # > / >> / &> targets (not >&)
_READ_ANYWHERE = re.compile(r"^\s*(cat|head|tail|wc|grep|rg|find|ls|tree|diff|sort|uniq|stat|file|du)\b")
_WRITE_ANYWHERE = re.compile(r"^\s*(tee|cp|mv|rsync|install|dd|ln|scp|curl|wget|chmod|chown)\b")
_RISKY = re.compile(
    r"\bfind\b[^|;&]*\s-(delete|exec|execdir|ok)\b|\bgit\s+branch\s+[^|;&]*-[dDmM]\b|"
    r"\bgit\s+push\b|\bxargs\b[^|;&]*\brm\b", re.I)


def _inside_root(target: str, root: Path | None) -> bool | None:
    """Is a path argument inside the workspace? Safe /dev sinks are fine.
    Returns None when no workspace is known and the path can't be vetted."""
    if root is None:
        return None
    t = target.strip().strip("'\"")
    if t.startswith(("~", "$HOME", "${HOME")):
        return False
    try:
        full = Path(os.path.expandvars(t))
        full = full if full.is_absolute() else (root / full)
        full = full.resolve()
    except (OSError, RuntimeError):
        return False
    if str(full) in ("/dev/null", "/dev/zero", "/dev/tty", "/dev/stdin",
                     "/dev/stdout", "/dev/stderr", "NUL"):
        return True
    return full == root or root in full.parents


def _paths_outside(part: str, root: Path | None) -> bool:
    """A read/write command (cat, grep, cp, tee, ...) pointing at paths outside the workspace.
    With no workspace to vet against, any path-shaped argument is treated as
    outside — a caller that can't prove containment doesn't get to pass."""
    if not (_READ_ANYWHERE.match(part) or _WRITE_ANYWHERE.match(part)):
        return False
    for tok in part.split()[1:]:
        if tok.startswith("-") or tok == "--":
            continue
        if "/" not in tok and "~" not in tok and "$" not in tok:
            continue
        if _inside_root(tok, root) is not True:
            return True
    return False


def needs_approval(command: str, policy: str, root: Path | None = None) -> bool | None:
    """True = ask the user, False = run, None = refuse outright.
    The policy only decides whether risky commands prompt; catastrophe and
    exfiltration checks (outside-workspace reads/writes, destructive constructs)
    apply under every policy, including never_ask."""
    norm = command.replace('"', "").replace("'", "")             # quotes don't hide targets
    if _NEVER.search(norm):
        return None
    unattended = policy == "never_ask"

    def blocked() -> bool | None:
        return None if unattended else True                     # can't ask with no human in the loop

    for inner in _SUBST_INNER.findall(command):
        # Command substitution can smuggle anything past the safe list: vet the
        # inner command with the same rules (e.g. `echo $(find ~ -delete)`).
        inner = inner[0] or inner[1]
        if inner and needs_approval(inner, policy, root) is not False:
            return blocked()
    if _SUBST.search(command) and not unattended:
        return True                                             # asks, even when inner commands looked safe
    for target in _REDIR.findall(command):
        inside = _inside_root(target, root)
        if inside is False:
            return blocked()                                    # provably outside the workspace
        if inside is None:
            # No workspace to vet against: ask, unless the user opted out of asking.
            return False if unattended else True
    if _RISKY.search(norm):
        return blocked()
    parts = [p.strip() for p in _SPLIT.split(command) if p.strip()]
    for p in parts:
        if _paths_outside(p, root):
            return blocked()
    if unattended:
        return False
    if policy == "always_ask":
        return True
    for p in parts:
        if not _SAFE.match(p):
            return True
    return False


_NAME_DROP = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CREDS|_PAT\b|\bPAT_|_DSN\b|\bDSN|"
                        r"MYSQL_PWD|PGPASS|_AUTH\b|\bAUTH_|SSH_|_PRIVATE|CERT_)", re.I)
_URL_CRED = re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s@]+@", re.I)


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items()
           if not _NAME_DROP.search(k) and not _URL_CRED.search(v)}
    env.update({"CI": "1", "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    # The GIT_CONFIG_* family (CI/Codespaces set COUNT+KEY_n+VALUE_n triples) partially matches the
    # name filter — a surviving GIT_CONFIG_COUNT makes every git call die with "missing config key".
    # Repo-scoping vars are dropped too: they would point our git calls (and agent commands) at the
    # wrong repository. User config still comes through HOME.
    env = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG_")}
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(k, None)
    # Subscription/provider overrides: an exported promptaro-env or eval session would
    # silently reroute spawned commands (and the CLIs they run) through the gateway or
    # a different model. Name-matched keys/tokens are already gone; these aren't shaped
    # like secrets, so drop them by name.
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
              "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_BASE_URL", "GEMINI_BASE_URL",
              "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ROUTER_GATEWAY_TOKEN"):
        env.pop(k, None)
    return env


_SECRET_TEXT = re.compile(
    r"(sk-or-v1-[A-Za-z0-9_-]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|sk-proj-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|"
    r"gsk_[A-Za-z0-9]{16,}|csk-[A-Za-z0-9_-]{8,}|nvapi-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{16,}|"
    r"xox[bap]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|apikey|access[_-]?token|secret[_-]?(?:key|token)?|password|passwd)"
    r"[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_./+\-]{8,}[\"'])", re.I)

# File/folder names that almost always hold credentials — the read tools refuse them
# outright so a cloud model never sees their contents (even via a project chat on a
# real folder). Templates like .env.example stay blocked too: the same names usually
# carry real values in user folders.
_SENSITIVE_NAME = re.compile(
    r"^(\.env($|\..*)|\.ssh$|\.aws$|\.azure$|\.gnupg$|\.kube$|\.docker$|\.terraform$|"
    r"\.netrc$|\.npmrc$|\.pypirc$|\.git-credentials$|\.gitconfig-credentials$|"
    r"\.pgpass$|\.my?cnf$|config\.gcloud$|"
    r"(id_rsa|id_dsa|id_ecdsa|id_ed25519|id_xmss)(\.pub)?$|"
    r".*\.(pem|key|p12|pfx|jks|keystore|kubeconfig)$|"
    r"credentials?\.(json|xml|ya?ml|ini|cfg|env)$|secrets?\.(json|ya?ml|env)$)$", re.I)


def _sensitive(rel: Path) -> bool:
    return any(_SENSITIVE_NAME.match(part) for part in rel.parts)


def redact(text: str) -> str:
    """Provider errors sometimes echo the key that was sent — never show them."""
    return _SECRET_TEXT.sub(lambda m: m.group(0)[:7] + "…", str(text))


def clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n[... {len(text) - limit} characters omitted ...]\n\n{text[-half:]}"


class ToolError(Exception):
    pass


@dataclass
class ToolResult:
    ok: bool
    output: str
    meta: dict


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path(self, p: str) -> Path:
        if not isinstance(p, str) or not p.strip():
            raise ToolError("path is required")
        full = (self.root / p).resolve()
        if full != self.root and self.root not in full.parents:
            raise ToolError(f"'{p}' is outside the project folder")
        rel = full.relative_to(self.root)
        if any(part.lower() == ".git" for part in rel.parts):
            raise ToolError("the .git folder is off limits")
        if _sensitive(rel):
            raise ToolError(f"'{p}' looks like it holds credentials — the model can't see it")
        return full

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."

    # ------------------------------------------------------------- read
    async def list_files(self, path: str = ".", depth: int = 2) -> ToolResult:
        base = self.path(path)
        depth = max(1, min(int(depth), 6))
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-files", "--cached", "--others", "--exclude-standard", cwd=base,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        files = out.decode(errors="replace").splitlines() if proc.returncode == 0 else [
            str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and ".git" not in p.parts][:5000]
        files = [f for f in files if not _sensitive(Path(f))]
        shown, dirs = [], set()
        for f in sorted(files):
            parts = f.split("/")
            if len(parts) > depth:
                d = "/".join(parts[:depth]) + "/"
                if d not in dirs:
                    dirs.add(d)
                    shown.append(d)
            else:
                shown.append(f)
        return ToolResult(True, clip("\n".join(shown) or "(empty)"), {"count": len(files)})

    async def read_file(self, path: str, start: int = 1, end: int | None = None) -> ToolResult:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"{path} does not exist")
        if p.stat().st_size > 2_000_000:
            raise ToolError(f"{path} is larger than 2 MB; use search instead")
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end) if end else start + READ_LINES - 1)
        body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n[lines {end + 1}-{len(lines)} not shown]" if end < len(lines) else ""
        return ToolResult(True, clip(redact(body + more)),
                          {"lines": len(lines), "start": start, "end": end})

    async def search(self, pattern: str, path: str = ".") -> ToolResult:
        base = self.path(path)
        if shutil.which("rg"):
            args = ["rg", "-n", "--no-heading", "--max-count", "50",
                    "-g", "!.env*", "-g", "!*.pem", "-g", "!*.key", "-g", "!id_*",
                    "-g", "!.ssh", "-g", "!.aws", "-e", pattern, str(base)]
        else:
            args = ["git", "grep", "-n", "-I", "-E", pattern, "--", str(base)]
        proc = await asyncio.create_subprocess_exec(*args, cwd=self.root, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        text = out.decode(errors="replace").replace(str(self.root) + "/", "")
        lines = [ln for ln in text.splitlines()
                 if not _sensitive(Path(ln.split(":", 1)[0].strip()))][:200]
        return ToolResult(True, clip(redact("\n".join(lines)) or "(no matches)"),
                          {"matches": len(lines)})

    # ------------------------------------------------------------ write
    async def write_file(self, path: str, content: str) -> ToolResult:
        p = self.path(path)
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else str(content))
        return ToolResult(True, f"{'Replaced' if existed else 'Created'} {self.rel(p)} ({len(content.splitlines())} lines)",
                          {"path": self.rel(p), "created": not existed})

    async def edit_file(self, path: str, old: str, new: str) -> ToolResult:
        p = self.path(path)
        if not p.is_file():
            raise ToolError(f"{path} does not exist")
        text = p.read_text(errors="replace")
        n = text.count(old) if old else 0
        if n == 0:
            raise ToolError("`old` text not found — read the file again and copy the snippet exactly")
        if n > 1:
            raise ToolError(f"`old` text appears {n} times — include more surrounding lines so it is unique")
        p.write_text(text.replace(old, new, 1))
        return ToolResult(True, f"Edited {self.rel(p)}", {"path": self.rel(p)})

    # -------------------------------------------------------------- run
    async def run(self, command: str, timeout: int = 120) -> ToolResult:
        timeout = max(5, min(int(timeout or 120), 900))
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self.root, env=_clean_env(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, **kwargs)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            code = proc.returncode
        except asyncio.TimeoutError:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)      # kill the whole pipeline, not just the shell
                else:
                    proc.kill()                            # Windows has no killpg
            except (ProcessLookupError, PermissionError):
                pass
            await proc.wait()
            return ToolResult(False, f"Timed out after {timeout}s", {"exit_code": None, "timeout": True})
        text = out.decode(errors="replace")
        return ToolResult(code == 0, clip(f"$ {command}\n{text}\n[exit code {code}]"), {"exit_code": code})

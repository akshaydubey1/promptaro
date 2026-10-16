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
_NEVER = re.compile(r"(\bsudo\b|rm\s+-rf\s+(/|~)(\s|$)|:\(\)\s*\{|\bmkfs\b|\bdd\s+if=|>\s*/dev/sd)")


def needs_approval(command: str, policy: str) -> bool | None:
    """True = ask the user, False = run, None = refuse outright."""
    if _NEVER.search(command):
        return None
    if policy == "never_ask":
        return False
    if policy == "always_ask":
        return True
    if "`" in command or "$(" in command or re.search(r"(^|[^>])>(?!&)", command):
        return True                                  # substitution or file redirection
    if re.search(r"\bfind\b.*\s-(delete|exec|execdir|ok)\b|\bgit\s+branch\s+.*-[dDmM]\b", command):
        return True
    parts = [p for p in re.split(r"&&|\|\||;|\|", command) if p.strip()]
    return not all(_SAFE.match(p) for p in parts)


def _clean_env() -> dict:
    drop = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
    env = {k: v for k, v in os.environ.items() if not drop.search(k)}
    env.update({"CI": "1", "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


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
        if any(part == ".git" for part in full.relative_to(self.root).parts):
            raise ToolError("the .git folder is off limits")
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
        return ToolResult(True, clip(body + more), {"lines": len(lines), "start": start, "end": end})

    async def search(self, pattern: str, path: str = ".") -> ToolResult:
        base = self.path(path)
        if shutil.which("rg"):
            args = ["rg", "-n", "--no-heading", "--max-count", "50", "-e", pattern, str(base)]
        else:
            args = ["git", "grep", "-n", "-I", "-E", pattern, "--", str(base)]
        proc = await asyncio.create_subprocess_exec(*args, cwd=self.root, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        text = out.decode(errors="replace").replace(str(self.root) + "/", "")
        lines = text.splitlines()[:200]
        return ToolResult(True, clip("\n".join(lines) or "(no matches)"), {"matches": len(lines)})

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
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self.root, env=_clean_env(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
            code = proc.returncode
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return ToolResult(False, f"Timed out after {timeout}s", {"exit_code": None, "timeout": True})
        text = out.decode(errors="replace")
        return ToolResult(code == 0, clip(f"$ {command}\n{text}\n[exit code {code}]"), {"exit_code": code})

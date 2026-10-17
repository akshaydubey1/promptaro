# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Use the subscriptions you already pay for. Author: Akshay Dubey

Two official coding tools can run on a plan you already have:

  * Claude Code (Anthropic) — signed in with a Claude Pro/Max plan.
  * Codex CLI (OpenAI)     — signed in with a ChatGPT plan.

When one is installed and signed in on this computer, Promptaro hands hard
agent tasks to it, so they run on your plan instead of per-use API billing.
If a plan hits its usage limit, the other plan (or the regular models) takes
over, and that plan is rested for an hour.

Promptaro never reads, stores or forwards either login: it runs the
unmodified program in the task's own copy of your project and reads its
progress output. Sign-in status comes from each tool's own status command.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .tools import clip

if TYPE_CHECKING:
    from .desk import Desk

HARD_TASK = 0.45          # complexity at or above this goes to a subscription
REST_S = 3600             # how long a plan rests after hitting its usage limit
ENGINES = ("claude_code", "codex")
MODEL_IDS = {"claude-code": "claude_code", "codex": "codex"}      # Model menu id -> engine
# Extra picks under each plan, e.g. "claude-code:opus" -> claude -p --model opus.
# Aliases ("opus", "fable") always map to the newest version on the plan; pinned
# names ("claude-opus-4") hold a specific generation. The set mirrors the model
# catalog bundled with current Claude Code / Codex releases — an entry the plan
# can't serve simply fails that run and the usual fallback picks up.
MODEL_CHOICES = {
    "claude_code": {
        "best": "Claude — best available",
        "fable": "Claude Fable (latest)",
        "mythos": "Claude Mythos (latest)",
        "opus": "Claude Opus (latest)",
        "sonnet": "Claude Sonnet (latest)",
        "haiku": "Claude Haiku (latest)",
        "claude-fable-5": "Claude Fable 5",
        "claude-mythos-5": "Claude Mythos 5",
        "claude-opus-5": "Claude Opus 5",
        "claude-sonnet-5": "Claude Sonnet 5",
        "claude-haiku-4": "Claude Haiku 4",
        "claude-opus-4": "Claude Opus 4",
        "claude-sonnet-4": "Claude Sonnet 4",
        "claude-sonnet-3": "Claude Sonnet 3",
        "claude-haiku-3": "Claude Haiku 3",
    },
    "codex": {
        "gpt-5.4": "GPT-5.4",
        "gpt-5.4-mini": "GPT-5.4 mini",
        "gpt-5.3-codex": "GPT-5.3 Codex",
        "gpt-5.3-codex-spark": "GPT-5.3 Codex Spark",
        "gpt-5.2-codex": "GPT-5.2 Codex",
        "gpt-5.2": "GPT-5.2",
        "gpt-5.1-codex-max": "GPT-5.1 Codex Max",
        "gpt-5.1-codex": "GPT-5.1 Codex",
        "gpt-5.1-codex-mini": "GPT-5.1 Codex mini",
        "gpt-5.1": "GPT-5.1",
        "gpt-5-codex": "GPT-5 Codex",
        "gpt-5-codex-mini": "GPT-5 Codex mini",
        "gpt-5": "GPT-5",
        "codex-mini-latest": "Codex mini (legacy)",
    },
}
LABEL = {"claude_code": "Claude Code", "codex": "Codex"}
PLAN = {"claude_code": "your Claude subscription", "codex": "your ChatGPT subscription"}


def engine_of(model_id: str) -> tuple[str, str]:
    """A model-menu id -> (engine, model variant): "claude-code:opus" -> ("claude_code", "opus")."""
    base, _, variant = (model_id or "").partition(":")
    return MODEL_IDS.get(base, ""), variant
_LIMIT_RE = re.compile(r"usage limit|rate.?limit|limit reached|quota|too many requests|\b429\b|out of credits", re.I)

_status_cache: dict[str, tuple[float, dict]] = {}
_rest_until: dict[str, float] = {}

# What Claude Code may do on its own inside the task's copy. Anything else is refused,
# because nobody is there to answer a permission prompt in this mode. Built-in
# Read/Grep/Glob/LS cover file reads — shell versions are left out because they can
# read anywhere on the disk, not just the workspace.
SAFE_TOOLS = [
    "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "LS", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)", "Bash(pytest:*)", "Bash(npm test:*)",
    "Bash(npm run test:*)", "Bash(npm run lint:*)", "Bash(npm run build:*)", "Bash(npx tsc:*)",
    "Bash(go test:*)", "Bash(go build:*)", "Bash(go vet:*)", "Bash(cargo test:*)", "Bash(cargo build:*)",
    "Bash(cargo check:*)", "Bash(make test:*)", "Bash(terraform fmt:*)", "Bash(terraform validate:*)",
    "Bash(ruff:*)", "Bash(mypy:*)",
]
NEVER_TOOLS = ["Bash(git push:*)", "Bash(git clean:*)", "Bash(sudo:*)", "Bash(rm -rf:*)", "Bash(rm -fr:*)",
               "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(gh:*)",
               "Bash(dd:*)", "Bash(mkfs:*)", "Bash(shred:*)", "Bash(chmod -R:*)", "Bash(chown:*)",
               "Bash(npm publish:*)", "Bash(yarn publish:*)", "Bash(cargo publish:*)",
               "Bash(kubectl apply:*)", "Bash(kubectl delete:*)",
               "Bash(terraform apply:*)", "Bash(terraform destroy:*)"]

_ACTION = {"Read": "read_file", "Edit": "edit_file", "MultiEdit": "edit_file", "Write": "write_file",
           "Bash": "run", "Grep": "search", "Glob": "search", "LS": "list_files"}

WORKER_NOTE = ("You are running inside Promptaro on an isolated git branch of the user's project. Stay inside "
               "this folder: only read or change files that live here. Make focused changes, run the project's "
               "tests when they exist, and end with a short summary of what changed and how you verified it.")


# ------------------------------------------------------------- finding the tools
def _find(env_vars: str | tuple, name: str, extra: list) -> str | None:
    for env_var in (env_vars if isinstance(env_vars, tuple) else (env_vars,)):
        override = os.environ.get(env_var)
        if override:                                       # an explicit override wins, or disables
            return override if Path(override).exists() else None
    for c in (shutil.which(name), *extra):
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def find_claude() -> str | None:
    home = Path.home()
    return _find(("PROMPTARO_CLAUDE_BIN", "TILLWRIGHT_CLAUDE_BIN"), "claude", [home / ".local/bin/claude", home / ".claude/local/claude",
                                                       "/opt/homebrew/bin/claude", "/usr/local/bin/claude"])


def find_codex() -> str | None:
    home = Path.home()
    return _find(("PROMPTARO_CODEX_BIN", "TILLWRIGHT_CODEX_BIN"), "codex", [home / ".local/bin/codex", home / ".npm-global/bin/codex",
                                                     "/opt/homebrew/bin/codex", "/usr/local/bin/codex"])


def _worker_env() -> dict:
    """Make the tool use your plan sign-in, not API keys or a gateway URL in the environment.
    Base-URL overrides (exported by promptaro-env / eval) would silently proxy the tool's
    subscription through the per-use gateway; anything that smells like a secret is dropped too.
    Starts from _clean_env so git's CI/config and repo-pointer vars can't corrupt the CLIs'
    own git calls either."""
    from .tools import _clean_env
    env = _clean_env()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
              "GEMINI_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
              "OPENAI_BASE_URL", "OPENAI_API_BASE", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
              "ROUTER_GATEWAY_TOKEN"):
        env.pop(k, None)
    env.update({"CI": "1", "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


async def _run_status(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, env=_worker_env(), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 15)
    return proc.returncode or 0, out.decode(errors="replace")


async def claude_status(force: bool = False) -> dict:
    """{installed, logged_in, subscription, path, auth_method, resting_s} — cached for a minute."""
    hit = _status_cache.get("claude_code")
    if hit and not force and time.time() - hit[0] < 60:
        return {**hit[1], "resting_s": resting("claude_code")}
    exe = find_claude()
    st = {"installed": bool(exe), "path": exe, "logged_in": False, "subscription": False, "auth_method": ""}
    if exe:
        try:
            _, out = await _run_status([exe, "auth", "status", "--json"])
            d = json.loads(out[out.find("{"): out.rfind("}") + 1] or "{}")
            st["logged_in"] = bool(d.get("loggedIn"))
            st["auth_method"] = str(d.get("authMethod") or "")
            # Signed in with a Claude account (OAuth) = subscription; an API key would bill per use.
            st["subscription"] = st["logged_in"] and "api" not in st["auth_method"].lower()
        except Exception:
            pass
    _status_cache["claude_code"] = (time.time(), st)
    return {**st, "resting_s": resting("claude_code")}


async def codex_status(force: bool = False) -> dict:
    hit = _status_cache.get("codex")
    if hit and not force and time.time() - hit[0] < 60:
        return {**hit[1], "resting_s": resting("codex")}
    exe = find_codex()
    st = {"installed": bool(exe), "path": exe, "logged_in": False, "subscription": False, "auth_method": ""}
    if exe:
        try:
            code, out = await _run_status([exe, "login", "status"])
            low = out.lower()
            st["logged_in"] = code == 0 and "logged in" in low and "not logged in" not in low
            st["auth_method"] = "chatgpt" if "chatgpt" in low else ("api_key" if "api key" in low else "")
            st["subscription"] = st["logged_in"] and st["auth_method"] == "chatgpt"
        except Exception:
            pass
    _status_cache["codex"] = (time.time(), st)
    return {**st, "resting_s": resting("codex")}


async def subscriptions(force: bool = False) -> dict:
    cc, cx = await asyncio.gather(claude_status(force), codex_status(force))
    return {"claude_code": cc, "codex": cx}


# --------------------------------------------------------------- plan rest
def resting(engine: str) -> int:
    return max(0, int(_rest_until.get(engine, 0) - time.time()))


def rest(engine: str, seconds: float = REST_S) -> None:
    _rest_until[engine] = time.time() + seconds


def hit_limit(text: str) -> bool:
    return bool(_LIMIT_RE.search(text or ""))


async def usable_engines() -> list[str]:
    """Signed-in plans that aren't resting, in preference order."""
    subs = await subscriptions()
    return [e for e in ENGINES if subs[e]["subscription"] and not resting(e)]


# ------------------------------------------------------------------ shared
def _tool_args(name: str, inp: dict) -> dict:
    a = {}
    if "file_path" in inp:
        a["path"] = inp["file_path"]
    if "path" in inp:
        a["path"] = inp["path"]
    if "command" in inp:
        a["command"] = inp["command"]
    if "pattern" in inp:
        a["pattern"] = inp["pattern"]
    if name in ("Edit", "MultiEdit") and "new_string" in inp:
        a["new"] = inp["new_string"][:1500]
    if name == "Write" and "content" in inp:
        a["content"] = inp["content"][:1500]
    return a or {k: str(v)[:200] for k, v in list(inp.items())[:3]}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content)


class _CliRun:
    """Runs one agent turn through an official coding tool and mirrors its progress into Promptaro."""
    engine = ""

    def __init__(self, desk: "Desk", sid: str):
        self.desk, self.sid = desk, sid
        self.store = desk.store
        self.limited = False            # True when the plan's usage limit stopped the run
        self.variant = ""               # per-model pick under the plan ("claude-code:opus" -> "opus")

    def args(self, exe: str, text: str, s: dict, st: dict) -> list[str]:
        raise NotImplementedError

    def handle(self, ev: dict, st: dict) -> None:
        raise NotImplementedError

    def outcome(self) -> tuple[bool, str, dict]:
        """(succeeded, summary or failure reason, usage {tokens_in, tokens_out})."""
        raise NotImplementedError

    def exe(self) -> str | None:
        raise NotImplementedError

    async def run(self, text: str) -> bool:
        """True if the tool finished the turn; False means: fall back (other plan or the router agent)."""
        store, sid = self.store, self.sid
        s = store.get(sid)
        st = s["state"] or {}
        exe = self.exe()
        if not exe:
            return False
        eng, var = engine_of(s.get("model"))
        self.variant = var if eng == self.engine else ""    # a fallback engine ignores another plan's pick
        name = LABEL[self.engine] if not self.variant else MODEL_CHOICES[self.engine].get(self.variant, self.variant)
        store.update(sid, status="running")
        store.emit(sid, "model", {"model": s.get("model") or self.engine.replace("_", "-"), "name": name,
                                  "via": PLAN[self.engine], "subscription": True,
                                  "why": f"hard task, so it runs on {PLAN[self.engine]} instead of per-use billing",
                                  "explain": {"level": "hard", "because": "hard agent work runs on a plan you "
                                              "already pay for, so it adds no per-use cost.",
                                              "subscription": True}})
        try:
            proc = await asyncio.create_subprocess_exec(*self.args(exe, text, s, st), cwd=s["worktree"],
                                                        env=_worker_env(), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
                                                        limit=16 * 1024 * 1024)
        except Exception as e:
            store.emit(sid, "note", {"text": f"{LABEL[self.engine]} couldn't start "
                                             f"({type(e).__name__}). Trying the next option."})
            return False
        # Drain stderr concurrently — a full stderr pipe would deadlock the run.
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    self.handle(json.loads(line), st)
                except json.JSONDecodeError:
                    continue
            await proc.wait()
        except asyncio.CancelledError:
            proc.terminate()
            raise
        except Exception as e:                                # spawn/stream failure: try the next option
            store.emit(sid, "note", {"text": f"{LABEL[self.engine]} stopped unexpectedly "
                                             f"({type(e).__name__}). Trying the next option."})
            return False
        finally:
            store.update(sid, state=st)
            if not err_task.done():
                err_task.cancel()

        ok, summary, usage = self.outcome()
        err = ""
        if not ok:
            err = (await err_task).decode(errors="replace")[-600:].strip()
        if usage:
            store.add_usage(sid, 0.0, usage.get("tokens_in", 0), usage.get("tokens_out", 0))   # included in plan
            self.desk.count_subscription_run(sid, usage)
        if ok:
            store.emit(sid, "finish", {"summary": summary[:6000] or "Done.", "tests": "not_run",
                                       "subscription": True})
            store.update(sid, status="review")
            return True
        reason = summary or err or f"exit {proc.returncode}"
        if hit_limit(reason):
            self.limited = True
            rest(self.engine)
            store.emit(sid, "note", {"text": f"{PLAN[self.engine].capitalize()} hit its usage limit, so it's resting "
                                             "for an hour. Carrying on with the next option."})
        else:
            store.emit(sid, "note", {"text": f"{LABEL[self.engine]} didn't complete ({reason[:400]}). "
                                             "Carrying on with the next option."})
        return False


class ClaudeCodeRun(_CliRun):
    engine = "claude_code"

    def __init__(self, desk, sid):
        super().__init__(desk, sid)
        self.pending: dict[str, str] = {}
        self.result: dict | None = None

    def exe(self):
        return find_claude()

    def args(self, exe, text, s, st):
        a = [exe, "-p", text, "--output-format", "stream-json", "--verbose", "--append-system-prompt", WORKER_NOTE]
        if self.variant:
            a += ["--model", self.variant]
        if (s.get("approval") or "ask_risky") == "never_ask":
            a += ["--permission-mode", "bypassPermissions", "--disallowedTools", *NEVER_TOOLS]
        else:
            a += ["--permission-mode", "acceptEdits", "--allowedTools", *SAFE_TOOLS, "--disallowedTools", *NEVER_TOOLS]
        if st.get("claude_session"):
            a += ["--resume", st["claude_session"]]
        return a

    def handle(self, ev, st):
        store, sid, kind = self.store, self.sid, ev.get("type")
        if kind == "system" and ev.get("session_id"):
            st["claude_session"] = ev["session_id"]
        elif kind == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    store.emit(sid, "thought", {"text": c["text"][:2000]})
                elif c.get("type") == "tool_use":
                    name = c.get("name", "tool")
                    self.pending[c.get("id", "")] = name
                    store.emit(sid, "tool_call", {"action": _ACTION.get(name, name),
                                                  "args": _tool_args(name, c.get("input") or {})})
        elif kind == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    name = self.pending.pop(c.get("tool_use_id", ""), "tool")
                    store.emit(sid, "tool_result", {"action": _ACTION.get(name, name),
                                                    "ok": not c.get("is_error", False),
                                                    "output": clip(_text_of(c.get("content")), 4000)})
        elif kind == "result":
            self.result = ev

    def outcome(self):
        r = self.result
        if not r:
            return False, "", {}
        u = r.get("usage") or {}
        usage = {"tokens_in": int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
                 + int(u.get("cache_creation_input_tokens", 0)), "tokens_out": int(u.get("output_tokens", 0))}
        if r.get("is_error"):
            return False, str(r.get("result") or r.get("subtype") or ""), usage
        return True, str(r.get("result") or "Done."), usage


class CodexRun(_CliRun):
    """OpenAI's Codex CLI (`codex exec --json`), signed in with a ChatGPT plan."""
    engine = "codex"

    def __init__(self, desk, sid):
        super().__init__(desk, sid)
        self.started: set[str] = set()
        self.last_message = ""
        self.done = False
        self.failure = ""
        self.usage: dict = {}

    def exe(self):
        return find_codex()

    def args(self, exe, text, s, st):
        # workspace-write: may edit files inside the task's copy only; no network. `exec` never stops to ask.
        common = ["--json", "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"']
        if self.variant:
            common += ["-m", self.variant]
        prompt = f"{WORKER_NOTE}\n\n{text}"
        if st.get("codex_session"):
            return [exe, "exec", "resume", *common, st["codex_session"], prompt]
        return [exe, "exec", *common, prompt]

    def _call(self, item: dict) -> None:
        if item.get("id") in self.started:
            return
        self.started.add(item.get("id", ""))
        t = item.get("type")
        if t == "command_execution":
            self.store.emit(self.sid, "tool_call", {"action": "run", "args": {"command": item.get("command", "")}})
        elif t == "file_change":
            paths = ", ".join(c.get("path", "") for c in item.get("changes") or []) or "files"
            self.store.emit(self.sid, "tool_call", {"action": "edit_file", "args": {"path": paths}})

    def handle(self, ev, st):
        t, store, sid = ev.get("type"), self.store, self.sid
        item = ev.get("item") or {}
        if t == "thread.started" and ev.get("thread_id"):
            st["codex_session"] = ev["thread_id"]
        elif t == "item.started" and item.get("type") in ("command_execution", "file_change"):
            self._call(item)
        elif t == "item.completed":
            kind = item.get("type")
            if kind in ("command_execution", "file_change"):
                self._call(item)
                if kind == "command_execution":
                    ok = item.get("exit_code") == 0 and item.get("status") != "failed"
                    store.emit(sid, "tool_result", {"action": "run", "ok": ok,
                                                    "output": clip(item.get("aggregated_output") or "", 4000),
                                                    "meta": {"exit_code": item.get("exit_code")}})
                else:
                    store.emit(sid, "tool_result", {"action": "edit_file", "ok": item.get("status") != "failed",
                                                    "output": ""})
            elif kind == "agent_message" and (item.get("text") or "").strip():
                self.last_message = item["text"]
                store.emit(sid, "thought", {"text": item["text"][:2000]})
            elif kind == "error" and item.get("message"):
                store.emit(sid, "note", {"text": f"Codex: {item['message'][:400]}"})
        elif t == "turn.completed":
            self.done = True
            u = ev.get("usage") or {}
            self.usage = {"tokens_in": int(u.get("input_tokens", 0)), "tokens_out": int(u.get("output_tokens", 0))}
        elif t == "turn.failed":
            self.failure = str((ev.get("error") or {}).get("message") or "turn failed")
        elif t == "error":
            self.failure = str(ev.get("message") or "error")

    def outcome(self):
        if self.done and not self.failure:
            return True, self.last_message or "Done.", self.usage
        return False, self.failure, self.usage


RUNNERS = {"claude_code": ClaudeCodeRun, "codex": CodexRun}

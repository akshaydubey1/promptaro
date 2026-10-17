# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""The coding agent. Author: Akshay Dubey

One loop per task: the model proposes one tool call, we run it, feed back the
result, repeat until `finish`. Every step goes through the Smart Router:

  * the planning step may use a stronger model than routine steps;
  * a model that keeps producing invalid actions or failing commands is
    swapped for the next model up the router's escalation ladder;
  * every step's cost, latency and success is recorded, so the router learns
    which models are good at agent work.

Token savers: the system prompt and conversation prefix are prompt-cached,
tool output is clipped, and old tool output is compacted in batches (batching
keeps the cached prefix stable between compactions).
"""
from __future__ import annotations

import asyncio
import json
import platform
import re
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from router import catalog, quota
from router.classifier import classify
from router.providers import ProviderError
from router.selector import route

from .tools import TOOL_DOCS, ToolError, Workspace, clip, needs_approval, redact

if TYPE_CHECKING:
    from .desk import Desk

ACTIONS = {"list_files", "read_file", "search", "write_file", "edit_file", "run", "finish"}
MAX_STEPS = 40
KEEP_RECENT = 6                 # tool results kept verbatim
COMPACT_AT_CHARS = 60_000       # compact older tool results once history passes this
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def system_prompt(ws: Workspace, listing: str, hints: list[str]) -> str:
    return f"""You are a senior software engineer working autonomously on the user's project.
You are inside an isolated git worktree at the project root; the user reviews your diff afterwards,
so make focused, minimal, correct changes and don't reformat unrelated code.
OS: {platform.system()}.

{TOOL_DOCS}
Start by understanding the relevant code (search/read), then change it, then verify
(run tests, linters or a quick check). When done, call finish with a short summary of what
changed and how you verified it.
{("Project hints: " + "; ".join(hints)) if hints else ""}

Top-level files:
{listing}
"""


def project_hints(root: Path) -> list[str]:
    import shutil
    h = []
    py = "python" if shutil.which("python") else "python3"
    if not shutil.which("python"):
        h.append("Use `python3` (there is no `python` command here)")
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or (root / "tests").is_dir() \
            or any(root.glob("test_*.py")):
        h.append(f"Python — tests likely run with `{py} -m pytest -q`")
    if (root / "package.json").exists():
        try:
            scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
            if "test" in scripts:
                h.append("Node — tests run with `npm test`")
        except Exception:
            pass
    if (root / "go.mod").exists():
        h.append("Go — `go test ./...`")
    if (root / "Cargo.toml").exists():
        h.append("Rust — `cargo test`")
    if any(root.glob("*.tf")):
        h.append("Terraform — validate with `terraform fmt -check && terraform validate`")
    if (root / "Makefile").exists():
        h.append("Makefile present")
    return h


def parse_action(text: str) -> tuple[dict | None, str]:
    m = _JSON_BLOCK.search(text)
    raw = m.group(1) if m else None
    if raw is None:
        start, end = text.find("{"), text.rfind("}")
        raw = text[start:end + 1] if start != -1 and end > start else None
    if not raw:
        return None, "no JSON object found"
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e.msg} at char {e.pos})"
    if not isinstance(d, dict) or d.get("action") not in ACTIONS:
        return None, f"`action` must be one of {sorted(ACTIONS)}"
    if not isinstance(d.get("args", {}), dict):
        return None, "`args` must be an object"
    d.setdefault("args", {})
    return d, ""


def compact(messages: list[dict]) -> list[dict]:
    if sum(len(m["content"]) for m in messages) < COMPACT_AT_CHARS:
        return messages
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"].startswith("Result of ")]
    old = set(tool_idx[:-KEEP_RECENT])
    out = []
    for i, m in enumerate(messages):
        c = m["content"]
        if i in old and len(c) > 700:
            c = c[:350] + "\n[... older output trimmed to save tokens; re-run the tool if you need it ...]\n" + c[-200:]
        out.append({"role": m["role"], "content": c})
    return out


def _ui_args(action: str, args: dict) -> dict:
    a = dict(args)
    for k in ("content", "new", "old"):
        if isinstance(a.get(k), str) and len(a[k]) > 1500:
            a[k] = a[k][:1500] + f"\n… ({len(args[k])} chars)"
    return a


class AgentRun:
    def __init__(self, desk: "Desk", sid: str):
        self.desk, self.sid = desk, sid
        self.store = desk.store
        s = self.store.get(sid)
        self.s = s
        self.ws = Workspace(s["worktree"])
        self.state = s["state"] or {}
        self.messages: list[dict] = self.state.get("messages", [])
        self.level = int(self.state.get("level", 0))
        self.max_cost = float(self.state.get("max_cost", 2.0))

    def _save(self) -> None:
        self.state.update(messages=self.messages, level=self.level)
        self.store.update(self.sid, state=self.state)

    def _pick(self, task: str, planning: bool):
        r = self.desk.router
        prof = classify(task)
        prof.task_type = "coding"
        if planning:
            prof.complexity = min(1.0, prof.complexity + 0.2)
        force = self.s.get("model") if self.s.get("model") not in ("", None, "claude-code", "codex") else None
        from router.catalog import get_settings
        mode = get_settings().get("free_models", "chat")
        # Private code: free models only from providers that don't train on prompts, unless allowed.
        # Task budget and the global daily cap both limit paid models; free models always fit.
        cap = max(0.0, min(self.max_cost, r._daily_budget_left()))
        d = route(r.registry, r.store, prof, prefer=self.s.get("prefer") or "balanced", force_model=force,
                  allow_free=mode != "off", private=mode != "all", max_cost_usd=cap)
        ladder = [d.primary] + d.ladder
        cand = ladder[min(self.level, len(ladder) - 1)]
        return cand.model, prof, d, len(ladder)

    async def _approve(self, command: str) -> bool:
        aid = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.desk.approvals[self.sid] = (aid, fut)
        self.store.update(self.sid, status="waiting")
        self.store.emit(self.sid, "approval", {"id": aid, "command": command})
        try:
            ok = await fut
        finally:
            self.desk.approvals.pop(self.sid, None)
        self.store.update(self.sid, status="running")
        self.store.emit(self.sid, "approval_result", {"id": aid, "approved": ok})
        return ok

    async def _tool(self, action: str, args: dict):
        ws = self.ws
        if action == "list_files":
            return await ws.list_files(args.get("path", "."), args.get("depth", 2))
        if action == "read_file":
            return await ws.read_file(args.get("path", ""), args.get("start", 1), args.get("end"))
        if action == "search":
            return await ws.search(str(args.get("pattern", "")), args.get("path", "."))
        if action == "write_file":
            return await ws.write_file(args.get("path", ""), args.get("content", ""))
        if action == "edit_file":
            return await ws.edit_file(args.get("path", ""), args.get("old", ""), args.get("new", ""))
        if action == "run":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                raise ToolError("command is required")
            need = needs_approval(cmd, self.s.get("approval") or "ask_risky", self.ws.root)
            if need is None:
                raise ToolError("That command is blocked for safety.")
            if need and not await self._approve(cmd):
                raise ToolError("The user declined this command. Try another approach or finish and explain.")
            return await ws.run(cmd, args.get("timeout", 120))
        raise ToolError(f"unknown action {action}")

    async def run(self, user_text: str) -> None:
        sid, store = self.sid, self.store
        task = self.state.get("task") or user_text
        self.state.setdefault("task", task)
        if not self.messages:
            listing = (await self.ws.list_files(".", 1)).output
            self.state["system"] = system_prompt(self.ws, listing, project_hints(self.ws.root))
            self.messages.append({"role": "user", "content": f"Task: {user_text}"})
        else:
            self.messages.append({"role": "user", "content": f"User follow-up: {user_text}"})
        store.update(sid, status="running")
        invalid = fail_streak = errors = 0
        last_model = None
        r = self.desk.router

        for step in range(MAX_STEPS):
            for extra in self.desk.inbox.pop(sid, []):
                self.messages.append({"role": "user", "content": f"User message while you work: {extra}"})
                store.emit(sid, "note", {"text": "Delivered your message to the agent."})
            if (store.get(sid) or {}).get("cost", 0) >= self.max_cost:
                store.emit(sid, "finish", {"summary": f"Stopped: reached this task's ${self.max_cost:.2f} budget. "
                                                      "Raise it and send 'continue' to keep going.", "tests": "not_run"})
                break

            planning = step == 0 and len(self.messages) <= 2
            spec, prof, decision, ladder_len = self._pick(task, planning)
            if spec.id != last_model:
                why = decision.why()
                if self.level:
                    why["because"] = "the previous model kept having problems, so this step moved up a level."
                elif planning:
                    why["because"] = "the first step plans the work, so it gets a stronger model than routine steps."
                store.emit(sid, "model", {"model": spec.id, "name": spec.extra.get("name", spec.id),
                                          "via": spec.extra.get("via", ""), "free": bool(spec.extra.get("free")),
                                          "why": "planning step" if planning else
                                          ("escalated after repeated problems" if self.level else decision.reason),
                                          "explain": why})
                last_model = spec.id
            strongest = max(decision.considered, key=lambda c: c.quality).model
            if spec.provider == "offline":
                store.emit(sid, "error", {"text": "No AI model is connected. Open Connections to sign in, or use "
                                                  "the manual tools (run commands, review, commit) below."})
                break

            self.messages = compact(self.messages)
            res = None
            if spec.extra.get("quota"):
                # Reserve free-allowance space up front so concurrent runs can't overshoot it.
                est = sum(len(m["content"]) for m in self.messages) // 4 + 6000
                ok, why, res = quota.LEDGER.begin(spec, est)
                if not ok:
                    if self.level < ladder_len - 1:
                        self.level += 1
                        store.emit(sid, "note", {"text": f"{spec.id}: {why} Trying the next option."})
                        continue
                    store.emit(sid, "finish", {"summary": f"Stopped: {why} Connect a paid provider or wait for "
                                                          "the free allowance to reset.", "tests": "not_run"})
                    break
            t0 = time.perf_counter()
            try:
                comp = await r.providers[spec.provider].complete(spec, self.state["system"], "", 6000,
                                                                 messages=self.messages)
                r.registry.breaker.record(spec.id, True)
                errors = 0
                if spec.extra.get("quota"):
                    quota.LEDGER.settle(res, spec, comp.tokens_in + comp.tokens_out, comp.headers)
                elif spec.extra.get("free"):
                    catalog.FREE.note_used()
            except (ProviderError, Exception) as e:            # network, auth, rate limit
                quota.LEDGER.abandon(res)
                if quota.after_error(spec, e):
                    pass                                        # free allowance: route around it next step
                elif spec.extra.get("free") and "429" in str(e):
                    catalog.FREE.note_rate_limited(str(e))
                else:
                    r.registry.breaker.record(spec.id, False)
                errors += 1
                store.emit(sid, "error", {"text": f"{spec.id}: {redact(e)[:300]}"})
                if errors >= 3:
                    store.emit(sid, "finish", {"summary": "Stopped: model calls keep failing. Check Connections.",
                                               "tests": "not_run"})
                    break
                if self.level < ladder_len - 1:
                    self.level += 1
                await asyncio.sleep(1.5 * errors)
                continue

            cost = spec.cost(comp.tokens_in, comp.tokens_out, comp.cached_tokens)
            store.add_usage(sid, cost, comp.tokens_in, comp.tokens_out)
            r.store.add_spend(cost)
            top = strongest.cost(comp.tokens_in, comp.tokens_out, comp.cached_tokens)
            store.bump(spent=cost, saved=max(0.0, top - cost), tokens=comp.tokens_in + comp.tokens_out, agent_steps=1)
            action, err = parse_action(comp.text)
            r.store.record(request_id=sid, model_id=spec.id, task_type="coding", complexity=prof.complexity,
                           passed=action is not None, cost_usd=cost, latency_ms=(time.perf_counter() - t0) * 1000,
                           tokens_in=comp.tokens_in, tokens_out=comp.tokens_out, attempt=self.level + 1,
                           reasons=[err] if err else [])
            self.messages.append({"role": "assistant", "content": comp.text[:20000] or "(empty)"})

            if action is None:
                invalid += 1
                hint = " Your reply was cut off; use edit_file for smaller changes." if comp.stop_reason in (
                    "max_tokens", "length") else ""
                self.messages.append({"role": "user", "content":
                                      f"That wasn't a valid action: {err}.{hint} Reply with ONE JSON object."})
                if invalid >= 2 and self.level < ladder_len - 1:
                    self.level += 1
                    invalid = 0
                    store.emit(sid, "escalate", {"reason": "the model's replies weren't valid actions"})
                self._save()
                continue
            invalid = 0

            name, args = action["action"], action["args"]
            if action.get("thought"):
                store.emit(sid, "thought", {"text": str(action["thought"])[:2000]})
            if name == "finish":
                store.emit(sid, "finish", {"summary": str(args.get("summary", ""))[:6000],
                                           "tests": args.get("tests", "not_run")})
                break

            store.emit(sid, "tool_call", {"action": name, "args": _ui_args(name, args)})
            try:
                res = await self._tool(name, args)
                ok, out, meta = res.ok, res.output, res.meta
            except ToolError as e:
                ok, out, meta = False, f"Error: {e}", {}
            except Exception as e:                                  # never let one tool kill the run
                ok, out, meta = False, f"Error: {type(e).__name__}: {e}", {}
            store.emit(sid, "tool_result", {"action": name, "ok": ok, "output": clip(out, 4000), "meta": meta})
            self.messages.append({"role": "user", "content": f"Result of {name}:\n{out}"})

            fail_streak = 0 if ok else fail_streak + 1
            if fail_streak >= 3 and self.level < ladder_len - 1:
                self.level += 1
                fail_streak = 0
                store.emit(sid, "escalate", {"reason": "three failed steps in a row"})
            self._save()
        else:
            store.emit(sid, "finish", {"summary": f"Paused after {MAX_STEPS} steps. Send 'continue' to keep going.",
                                       "tests": "not_run"})

        self._save()
        store.update(sid, status="review")

# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Session manager: chats and agents, many at once. Author: Akshay Dubey"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path

from router.classifier import classify
from router.engine import Router, RunRequest

from . import gitops
from .agent import AgentRun
from .store import Store
from .tools import redact
from .workers import HARD_TASK, RUNNERS, engine_of, usable_engines

CHAT_HISTORY_TURNS = 8

# Lets the chat answer questions about Promptaro itself accurately.
APP_HELP = """You are the assistant inside Promptaro, a desktop app by Akshay Dubey. If the user asks how to use
Promptaro, answer from these facts only (don't invent features):
- New chat: ask anything; each answer shows the model used and its cost. The Model menu picks a specific model or
  Auto; Priority picks Balanced / Lowest cost / Fastest / Best quality.
- New project chat: a chat bound to a folder; the model reads that project's files to answer but can never
  change or run anything (read-only). A project must be a real subfolder — the home folder and whole drives are
  refused, and files that hold credentials (.env, .ssh, keys) are never shown to the model.
- The sidebar groups work by project: "Chats" holds general conversations and "Projects" lists each folder with
  its own chats and agent tasks; click a project name to fold it, or its + button to start a chat inside it.
- Hover any sidebar item for a pencil (rename the chat or task) and an x (delete); project headers have the same
  pencil (rename the project) and a + (new chat in it).
- Each conversation's header has buttons to download the whole conversation as Markdown, close the view, and
  delete it. Chats also have a small x in the sidebar for quick deletion.
- New agent task (button top-left): choose a project folder (must be a git repository; Promptaro offers
  "Set it up with git" if it isn't), describe the task, optionally set model, priority, command approvals and a
  dollar budget, then Start agent. The agent works on its own git branch in a separate copy of the project.
- While it works you see each step; you can message it or press Stop. Risky commands wait for Allow / Don't allow.
- When done: Review changes, then Merge into your branch, Open pull request (needs GitHub CLI `gh`), Commit to
  branch, Open in VS Code, or Discard.
- Connections (bottom-left): Sign in with OpenRouter, or paste Anthropic/OpenAI/Google keys; Ollama is detected
  automatically. Custom endpoints adds any OpenAI-compatible API (Ollama, LM Studio, vLLM, a company proxy) by
  base URL, with an optional key and a "Private" flag that lets code use it. Free providers (Groq, Cerebras,
  NVIDIA NIM, Mistral, Codestral, Cohere) each take one free key;
  Promptaro tracks each one's limits and shows what's left today. Providers that may train on prompts are only used
  for chats unless the user allows them for code. A daily $ budget caps all paid spend.
- Studio (sidebar): generates images and short videos on free tiers — Hugging Face, Together AI, Cloudflare
  Workers AI, and Pollinations which needs no key — best-first, and converts files locally: make or
  combine PDFs, convert images/audio/video/documents between formats. Nothing is uploaded anywhere.
  Google AI Studio image models may bill a key once free quota runs out, so they only run when the user turns
  on "Allow paid image models" in Studio.
- Coding tools (sidebar): run Claude Code, Aider, Cline, Continue or OpenCode on these models through Promptaro's
  local gateway. `promptaro-claude` starts Claude Code through it; `promptaro-aider` starts Aider;
  `eval "$(promptaro-env)"` sets the variables for any other tool (the old `tw-*` commands still work).
  By default coding tools only use free models (never spend money).
- Hard agent tasks run automatically on the user's Claude subscription (through Claude Code) or ChatGPT
  subscription (through Codex CLI) when that tool is installed and signed in — no per-use charge; the plan's usage
  limits apply, and when one plan hits its limit the other plan or the regular models take over. The Model menu
  can also pick a specific model under a plan — Claude Opus/Sonnet/Haiku (latest aliases or pinned Opus 5 /
  Sonnet 5) or Codex's GPT-5.x. A chat with a plan pick answers through that plan's tool, read-only. Easy chats use free
  models (OpenRouter allows 50 free requests a day, or 1000 after buying $10 of credit once); other work uses the
  cheapest paid model that can do it. Each answer shows the model, Free/Paid/Subscription, tokens used, and a
  "Why this model?" link with the options considered. Thumbs up/down teaches the router; thumbs down also offers
  "Try a stronger model". The Savings view (top bar) shows what smart routing saved.
- Promptaro was previously called Tillwright; old settings, keys, sessions and `tw-*` launchers still work.
- Common problems: "not a git repository" -> use Set it up with git; no models -> open Connections;
  an agent stops at its budget -> raise the budget and send "continue"."""


class Desk:
    def __init__(self, router: Router, store: Store):
        self.router, self.store = router, store
        self.tasks: dict[str, asyncio.Task] = {}
        self.approvals: dict[str, tuple[str, asyncio.Future]] = {}
        self.inbox: dict[str, list[str]] = {}
        self.gateway_log: deque = deque(maxlen=50)          # recent coding-tool requests, for the Coding tools view
        router.usage_listeners.append(self._gateway_usage)

    def _gateway_usage(self, spec, tin: int, tout: int, cost: float, top_cost: float, source: str) -> None:
        free = bool(spec.extra.get("free"))
        self.store.bump(spent=cost, saved=max(0.0, top_cost - cost), tokens=tin + tout, gateway_requests=1,
                        gateway_free=int(free))
        self.gateway_log.append({"ts": time.time(), "model": spec.id, "name": spec.extra.get("name", spec.id),
                                 "via": spec.extra.get("via", ""), "free": free, "tokens_in": tin,
                                 "tokens_out": tout, "cost": round(cost, 6)})

    # --------------------------------------------------------------- chat
    async def chat(self, sid: str, text: str, *, retry_of: int | None = None, exclude: tuple = ()) -> None:
        s = self.store.get(sid)
        if engine_of(s.get("model") or "")[0]:           # a subscription pick: the plan's own tool answers
            if await self._plan_chat(sid, text):
                return
            self.store.emit(sid, "note", {"text": "No signed-in plan could answer, so a regular "
                                                  "model is answering instead."})
        if s.get("project"):                            # a chat bound to a folder: read-only tools ground it
            from .agent import ChatRun
            await ChatRun(self, sid).run(text)
            return
        past = [e for e in self.store.events(sid) if e["kind"] in ("user", "assistant")]
        if retry_of is not None:                         # history up to the question being retried
            past = [e for e in past if e["seq"] < retry_of]
        past = past[-CHAT_HISTORY_TURNS * 2:]
        ctx = "\n\n".join(f"{'User' if e['kind'] == 'user' else 'Assistant'}: {e['data'].get('text', '')}"
                          for e in past[:-1])  # last one is the message being answered
        self.store.update(sid, status="running")
        forced = m if (m := s.get("model")) and not engine_of(m)[0] else None
        prefer = "best" if retry_of is not None else (s.get("prefer") or "balanced")
        last_flush = [0.0]
        buf: list[str] = []
        current = [0]

        def on_delta(attempt: int, chunk: str) -> None:
            # Batch tiny chunks (~20 updates a second) so the window stays smooth.
            if attempt != current[0]:
                if buf:
                    self.store.publish(sid, "delta", {"attempt": current[0], "text": "".join(buf)})
                    buf.clear()
                current[0] = attempt
                self.store.publish(sid, "delta", {"attempt": attempt, "text": "", "restart": attempt > 1})
            buf.append(chunk)
            now = time.monotonic()
            if now - last_flush[0] > 0.05:
                last_flush[0] = now
                self.store.publish(sid, "delta", {"attempt": attempt, "text": "".join(buf)})
                buf.clear()

        try:
            res = await self.router.run(RunRequest(
                instruction=text, context=("Earlier in this conversation:\n" + ctx) if ctx else "",
                prefer=prefer, force_model=forced, use_cache=not ctx and retry_of is None,
                system_extra=APP_HELP, exclude=exclude), on_delta=on_delta)
            if buf:
                self.store.publish(sid, "delta", {"attempt": current[0], "text": "".join(buf)})
            tin = sum(a.tokens_in for a in res.attempts)
            tout = sum(a.tokens_out for a in res.attempts)
            self.store.add_usage(sid, res.total_cost, tin, tout)
            reg = self.router.registry
            spec = reg.models.get(res.model or "")
            free = bool(spec and spec.extra.get("free"))
            why = dict(res.route.get("why") or {})
            strongest = reg.models.get((why.get("strongest") or {}).get("id", ""))
            saved = max(0.0, strongest.cost(tin, tout) - res.total_cost) if strongest and not res.cached else 0.0
            if res.cached and strongest:
                saved = strongest.cost(res.profile.get("est_input_tokens", 200), 300)
            why.update(saved_usd=round(saved, 6), escalations=max(0, len(res.attempts) - 1))
            if res.model != "offline":
                self.store.bump(spent=res.total_cost, saved=saved, tokens=tin + tout, answers=1,
                                answers_free=int(free), answers_paid=int(not free and not res.cached),
                                answers_cached=int(res.cached))
            self.store.emit(sid, "assistant", {
                "text": res.output, "model": res.model, "name": spec.extra.get("name") if spec else res.model,
                "passed": res.passed, "cached": res.cached, "cost": round(res.total_cost, 6),
                "calls": len(res.attempts), "task_type": res.profile.get("task_type"),
                "complexity": res.profile.get("complexity"),
                "why": res.route.get("reason", ""), "explain": why, "skills": res.skills,
                "free": free, "tokens_in": tin, "tokens_out": tout,
                "cost_note": spec.extra.get("cost_note", "") if spec else "",
                "via": spec.extra.get("via", "") if spec else "", "retry": retry_of is not None,
                "offline": (res.model == "offline")})
        except Exception as e:
            self.store.emit(sid, "error", {"text": redact(e)[:500]})
        finally:
            self.store.update(sid, status="idle")

    async def _plan_chat(self, sid: str, text: str) -> bool:
        """A chat picked a plan model: the official tool answers read-only. False -> router."""
        from .workers import CHAT_RUNNERS, usable_engines
        wanted, _ = engine_of(self.store.get(sid).get("model") or "")
        plans = await usable_engines()
        for eng in dict.fromkeys([wanted, *plans]):
            if eng in plans and eng in CHAT_RUNNERS:
                if await CHAT_RUNNERS[eng](self, sid).run(text):
                    return True
        return False

    # ----------------------------------------------------------- learning
    def feedback(self, sid: str, seq: int, good: bool) -> dict:
        """Thumbs up/down on an answer. The router learns from it straight away."""
        ev = self.store.find_event(sid, seq)
        if not ev or ev["kind"] != "assistant":
            raise KeyError("no such answer")
        d = ev["data"]
        if d.get("model") and d["model"] != "offline":
            self.router.feedback(d["model"], d.get("task_type") or "chat", float(d.get("complexity") or 0.3), good)
        self.store.emit(sid, "feedback", {"seq": seq, "good": good})
        self.store.bump(thumbs_up=int(good), thumbs_down=int(not good))
        return {"ok": True, "can_retry": not good}

    def retry_stronger(self, sid: str, seq: int) -> None:
        """Answer the same question again with a stronger model."""
        q = next((e for e in reversed(self.store.events(sid)) if e["kind"] == "user" and e["seq"] < seq), None)
        ans = self.store.find_event(sid, seq)
        if not q or not ans:
            raise KeyError("no question to retry")
        self.store.emit(sid, "note", {"text": "Trying again with a stronger model."})
        prev = ans["data"].get("model")
        asyncio.create_task(self.chat(sid, q["data"]["text"], retry_of=q["seq"] + 1,
                                      exclude=(prev,) if prev and prev != "offline" else ()))

    def learn_from_agent(self, sid: str, good: bool) -> None:
        """Merged work counts as a success for the models that did it; discarded finished work as a miss."""
        s = self.store.get(sid)
        task = (s["state"] or {}).get("task") or s["title"]
        cx = classify(task).complexity
        used = {e["data"].get("model") for e in self.store.events(sid)
                if e["kind"] == "model" and not e["data"].get("subscription")}
        for mid in used - {None, "offline"}:
            self.router.feedback(mid, "coding", cx, good, source="merged" if good else "discarded")

    def count_subscription_run(self, sid: str, usage: dict) -> None:
        """Value of a plan run: what the strongest pay-per-use model would have charged for the same tokens."""
        reg = self.router.registry
        paid = [m for m in reg.usable() if m.provider != "offline" and not m.extra.get("free")]
        top = max(paid, key=lambda m: (m.tier, m.price_out), default=None)
        value = top.cost(usage.get("tokens_in", 0), usage.get("tokens_out", 0)) if top else 0.0
        self.store.bump(saved=value, plan_value=value, tokens=usage.get("tokens_in", 0) + usage.get("tokens_out", 0),
                        subscription_runs=1)

    def insights(self) -> dict:
        t = self.store.totals()
        stats = self.router.store.stats()
        by_model: dict[str, dict] = {}
        for r in stats:
            b = by_model.setdefault(r["model"], {"runs": 0, "passes": 0, "up": 0, "down": 0})
            b["runs"] += r["runs"]
            b["passes"] += round((r["pass_rate"] or 0) * r["runs"])
            b["up"] += r.get("thumbs_up", 0)
            b["down"] += r.get("thumbs_down", 0)
        reg = self.router.registry
        learned = sorted(({"model": k, "name": reg.models[k].extra.get("name", k) if k in reg.models else k,
                           "runs": v["runs"], "pass_rate": round(v["passes"] / v["runs"], 2) if v["runs"] else None,
                           "thumbs_up": v["up"], "thumbs_down": v["down"]} for k, v in by_model.items()),
                         key=lambda x: -(x["runs"] + x["thumbs_up"] + x["thumbs_down"]))[:12]
        return {"totals": {k: round(v, 6) if isinstance(v, float) else v for k, v in t.items()}, "learned": learned}

    # -------------------------------------------------------------- agent
    async def start_agent(self, project: str, task: str, *, prefer: str, model: str, approval: str,
                          max_cost: float) -> dict:
        repo = await gitops.repo_root(project)
        s = self.store.create("agent", task.splitlines()[0][:80], project=str(repo), repo=str(repo),
                              prefer=prefer, model=model or "", approval=approval,
                              state={"max_cost": max_cost})
        try:
            info = await gitops.create_worktree(repo, s["id"], task)
        except Exception:
            self.store.delete(s["id"])                # no dead session left in the sidebar
            raise
        self.store.update(s["id"], worktree=info["worktree"], branch=info["branch"],
                          base_branch=info["base_branch"], base_sha=info["base_sha"])
        self.store.emit(s["id"], "user", {"text": task})
        self.store.emit(s["id"], "workspace", {"branch": info["branch"], "base": info["base_branch"],
                                               "note": "Your uncommitted changes are not included; the agent "
                                                       "starts from your last commit." if info["repo_dirty"] else ""})
        self._spawn(s["id"], task)
        return self.store.get(s["id"])

    async def choose_engine(self, sid: str, text: str) -> str:
        """Who does the work: a plan you already pay for (Claude Code / Codex) for hard tasks, else the router."""
        s = self.store.get(sid)
        st = s["state"] or {}
        if st.get("engine"):                          # keep the same engine for follow-ups
            return st["engine"]
        model = s.get("model") or ""
        engine = "router"
        wanted = engine_of(model)[0]
        if not model or wanted:
            plans = await usable_engines()
            if wanted and wanted in plans:
                engine = wanted
            elif plans and (wanted or classify(text).complexity >= HARD_TASK or s.get("prefer") == "best"):
                engine = plans[0]
        st["engine"] = engine
        self.store.update(sid, state=st)
        return engine

    def _spawn(self, sid: str, text: str) -> None:
        async def go():
            try:
                engine = await self.choose_engine(sid, text)
                tried: set[str] = set()
                while engine in RUNNERS and engine not in tried:
                    tried.add(engine)
                    if await RUNNERS[engine](self, sid).run(text):
                        return
                    # This plan couldn't finish (limit reached, not available): next plan, else the router.
                    nxt = [e for e in await usable_engines() if e not in tried]
                    engine = nxt[0] if nxt else "router"
                    st = self.store.get(sid)["state"]
                    st["engine"] = engine
                    self.store.update(sid, state=st)
                await AgentRun(self, sid).run(text)
            except asyncio.CancelledError:
                self.store.emit(sid, "finish", {"summary": "Stopped by you.", "tests": "not_run"})
                self.store.update(sid, status="review")
            except Exception as e:
                self.store.emit(sid, "error", {"text": f"Agent crashed: {type(e).__name__}: {e}"})
                self.store.update(sid, status="review")
            finally:
                self.tasks.pop(sid, None)
        self.tasks[sid] = asyncio.create_task(go())

    def message_agent(self, sid: str, text: str) -> str:
        self.store.emit(sid, "user", {"text": text})
        if sid in self.tasks:
            self.inbox.setdefault(sid, []).append(text)
            return "queued"
        self._spawn(sid, text)
        return "started"

    def stop(self, sid: str) -> None:
        t = self.tasks.get(sid)
        if t:
            t.cancel()
        a = self.approvals.get(sid)
        if a and not a[1].done():
            a[1].set_result(False)

    def approve(self, sid: str, aid: str, ok: bool) -> bool:
        a = self.approvals.get(sid)
        if not a or a[0] != aid or a[1].done():
            return False
        a[1].set_result(ok)
        return True

    # ------------------------------------------------- manual (no-AI) tools
    async def manual_run(self, sid: str, command: str) -> dict:
        from .tools import Workspace, needs_approval
        s = self.store.get(sid)
        if needs_approval(command, "never_ask", Path(s["worktree"])) is None:
            self.store.emit(sid, "note", {"text": "That command is blocked for safety."})
            return {"ok": False, "blocked": True}
        self.store.emit(sid, "tool_call", {"action": "run", "args": {"command": command}, "manual": True})
        res = await Workspace(s["worktree"]).run(command, 300)
        self.store.emit(sid, "tool_result", {"action": "run", "ok": res.ok, "output": res.output, "manual": True})
        return {"ok": res.ok}

    async def review(self, sid: str) -> dict:
        s = self.store.get(sid)
        if not s.get("worktree") or not Path(s["worktree"]).exists():
            return {"files": [], "patch": "", "gone": True}
        return await gitops.diff(s["worktree"], s["base_sha"])

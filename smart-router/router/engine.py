# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""The router: one instruction in, a checked result out. Author: Akshay Dubey

Flow:
  classify -> choose model + escalation ladder -> pick skills -> trim context
  -> cache lookup -> call model -> run checks -> (optional cross-review)
  -> escalate on failure -> record outcome (improves future routing) -> cache
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from . import catalog, quota, secrets
from . import checks as ck
from .classifier import TASK_TYPES, TaskProfile, classify, estimate_tokens, needs_for
from .context import fit_context
from .providers import Completion, Provider, ProviderError, build_providers
from .registry import ModelSpec, Registry
from .selector import route, trim_ladder
from .skills import SkillLibrary
from .store import Store

BASE_SYSTEM = ("You are a precise, efficient assistant. Do exactly what the task asks, no more. "
               "Be concise: no preamble, no restating the question.")

_OUT_BUDGET = {"coding": 1800, "research": 1600, "writing": 1000, "document": 1500, "data": 1200, "chat": 500}


@dataclass
class RunRequest:
    instruction: str
    context: str = ""
    has_images: bool = False
    tests: str | None = None                 # optional pytest code the result must pass
    prefer: str = "balanced"                 # balanced | cheap | fast | best
    quality_target: float | None = None
    max_cost_usd: float | None = None
    force_model: str | None = None
    use_cache: bool = True
    cross_review: str | None = None          # override config: off | auto | always
    dry_run: bool = False                    # route only, don't call a model
    allow_free: bool | None = None           # None = follow the "free models" setting
    system_extra: str = ""                   # caller-supplied instructions (e.g. app help)
    exclude: tuple[str, ...] = ()            # models not to use (e.g. one the user just rated down)
    private: bool = False                    # code: skip free providers that may train on prompts


@dataclass
class Attempt:
    model: str
    passed: bool
    score: float
    reasons: list[str]
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    kind: str = "generate"                   # generate | review
    error: str | None = None


@dataclass
class RunResult:
    request_id: str
    output: str
    model: str | None
    passed: bool
    profile: dict
    route: dict
    skills: list[str]
    context: dict
    attempts: list[Attempt] = field(default_factory=list)
    routing_ms: float = 0.0
    total_ms: float = 0.0
    cached: bool = False

    @property
    def total_cost(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id, "output": self.output, "model": self.model,
            "passed": self.passed, "cached": self.cached,
            "totals": {"cost_usd": round(self.total_cost, 6),
                       "tokens_in": sum(a.tokens_in for a in self.attempts),
                       "tokens_out": sum(a.tokens_out for a in self.attempts),
                       "model_calls": len(self.attempts),
                       "routing_ms": round(self.routing_ms, 3), "total_ms": round(self.total_ms, 1)},
            "profile": self.profile, "route": self.route, "skills": self.skills, "context": self.context,
            "attempts": [a.__dict__ for a in self.attempts],
        }


class Router:
    def __init__(self, root: str | Path = ".", db_path: str | Path | None = None,
                 providers: dict[str, Provider] | None = None, use_catalog: bool = True,
                 refresh_catalog: bool = True):
        root = Path(root)
        self._cfg_path, self._use_catalog = root / "config" / "models.yaml", use_catalog
        self.registry = Registry(self._cfg_path, use_catalog, refresh_catalog)
        self.skills = SkillLibrary.load(root / "skills")
        self.store = Store(db_path or root / "data" / "router.db")
        self._client = httpx.AsyncClient(timeout=self.registry.defaults.get("request_timeout_s", 120))
        self.providers = providers or build_providers(self._client)
        r = self.registry.routing
        self.max_attempts = int(r.get("max_attempts", 3))
        self.ctx_budget = int(r.get("context_budget_tokens", 24000))
        self.cache_ttl = float(r.get("cache_ttl_s", 86400))
        self.run_code = bool(r.get("run_generated_code", True))
        quota.use(catalog.HOME / "ledger.json")
        self.usage_listeners: list = []          # called with (spec, tokens_in, tokens_out, cost, top_cost, source)
        self._spend_cache = (0.0, 0.0)           # (refetch_at, today's spend) — budget check stays off the DB

    def _daily_budget_left(self) -> float:
        """Dollars still available today under the daily cap; inf when no cap is set."""
        cap = float(catalog.get_settings().get("daily_budget_usd") or 0)
        if cap <= 0:
            return math.inf
        now = time.time()
        if now >= self._spend_cache[0]:
            self._spend_cache = (now + 5, self.store.spend_today())
        return max(0.0, cap - self._spend_cache[1])

    def notify_usage(self, spec, tin: int, tout: int, source: str = "gateway") -> None:
        """Tell the app about usage that didn't go through run() (e.g. the coding-tools gateway)."""
        cost = spec.cost(tin, tout)
        self.store.add_spend(cost)
        paid = [m for m in self.registry.usable() if m.provider != "offline" and not m.extra.get("free")]
        top = max(paid, key=lambda m: (m.tier, m.price_out), default=None)
        top_cost = top.cost(tin, tout) if top else cost
        for fn in list(self.usage_listeners):
            try:
                fn(spec, tin, tout, cost, top_cost, source)
            except Exception:
                pass

    @staticmethod
    def _allow_free(req: "RunRequest") -> bool:
        if req.allow_free is not None:
            return req.allow_free
        from .catalog import get_settings
        return get_settings().get("free_models", "chat") in ("chat", "all")

    def reload(self) -> None:
        """Rebuild the model list, e.g. after a new connection is added."""
        self.registry = Registry(self._cfg_path, self._use_catalog)

    async def refresh_catalog(self) -> None:
        """Fetch fresh model lists off the critical path, then swap in the new registry.

        Startup builds the registry cache-only (refresh_catalog=False) so the app
        opens instantly; this finishes the real refresh in a worker thread."""
        try:
            await asyncio.to_thread(self.reload)
        except Exception as e:
            print(f"[router] model catalog refresh failed; keeping the cached list: {secrets.redact(e)}",
                  flush=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ plan
    def plan(self, req: RunRequest) -> tuple[TaskProfile, object, list, str, dict, float]:
        """Everything decided locally before any model call. Timed for the <100ms target."""
        t0 = time.perf_counter()
        profile = classify(req.instruction, req.context, req.has_images)
        ctx, ctx_info = fit_context(req.instruction, req.context, self.ctx_budget)
        skills = self.skills.select(req.instruction, profile)
        # Route on what will actually be sent (trimmed context + system + skills),
        # not the raw instruction + context.
        profile.est_input_tokens = (estimate_tokens(req.instruction) + estimate_tokens(ctx)
                                    + estimate_tokens(BASE_SYSTEM) + estimate_tokens(req.system_extra)
                                    + sum(s.tokens for s in skills))
        if "long_context" in profile.needs and profile.est_input_tokens <= 100_000:
            profile.needs.discard("long_context")        # trimming removed the need
        decision = route(self.registry, self.store, profile, prefer=req.prefer,
                         quality_override=req.quality_target, max_cost_usd=req.max_cost_usd,
                         force_model=req.force_model, allow_free=self._allow_free(req),
                         exclude=req.exclude, private=req.private)
        return profile, decision, skills, ctx, ctx_info, (time.perf_counter() - t0) * 1000

    def _secondary_pool(self, req: RunRequest, exclude_id: str | None = None) -> list[ModelSpec]:
        """Models allowed to see this request's content for helper calls (classify, review).
        Honors the same privacy/free rules as the primary choice."""
        pool = [m for m in self.registry.usable() if m.provider != "offline" and m.id != exclude_id]
        if req.private:
            pool = [m for m in pool if not (m.extra.get("free") and m.extra.get("trains"))]
        if not self._allow_free(req):
            pool = [m for m in pool if not m.extra.get("free")]
        if not catalog.paid_allowed():
            pool = [m for m in pool if not catalog.costs_money(m)]     # helpers never spend money either
        return pool

    async def _maybe_llm_classify(self, req: RunRequest, profile: TaskProfile) -> TaskProfile:
        thr = float(self.registry.routing.get("llm_classifier_below_confidence", 0.0))
        if profile.confidence >= thr:
            return profile
        pool = self._secondary_pool(req)
        if not pool:
            return profile
        sys_p = ("Classify the task. Reply with JSON only: {\"task_type\": one of "
                 f"{list(TASK_TYPES)}, \"complexity\": 0..1}}")
        c = await self._secondary_call(min(pool, key=lambda m: m.price_in + m.price_out),
                                       sys_p, req.instruction[:2000], 60, json_mode=True)
        if c is None:
            return profile
        try:
            d = json.loads(c.text[c.text.find("{"): c.text.rfind("}") + 1])
            if d.get("task_type") in TASK_TYPES:
                profile.task_type = d["task_type"]
                profile.complexity = float(d.get("complexity", profile.complexity))
                profile.confidence = 0.9
                # A changed task type needs different capability flags (tests, json, ...).
                profile.needs = needs_for(profile.task_type, req.instruction, req.has_images,
                                          profile.est_input_tokens)
        except Exception:
            pass
        return profile

    async def _secondary_call(self, m: ModelSpec, system: str, user: str, max_out: int,
                              **kw) -> Completion | None:
        """One helper call (classify/review). Counts against the free-tier ledger
        exactly like a primary call — secondary calls can't drain free quotas silently."""
        res = None
        if m.extra.get("quota"):
            ok, _why, res = quota.LEDGER.begin(m, 1200)
            if not ok:
                return None
        try:
            c = await self.providers[m.provider].complete(m, system, user, max_out, **kw)
            quota.LEDGER.settle(res, m, c.tokens_in + c.tokens_out, getattr(c, "headers", None))
            if m.extra.get("free") and not m.extra.get("quota"):
                catalog.FREE.note_used()
            return c
        except (ProviderError, httpx.HTTPError) as e:
            quota.LEDGER.abandon(res)
            quota.after_error(m, e)
            return None

    # ------------------------------------------------------------------- run
    def refresh_free_quota(self) -> None:
        """Check the free allowance in the background; never on the request's critical path."""
        if time.time() - catalog.FREE.checked >= catalog.FREE.REFRESH_S:
            try:
                asyncio.get_running_loop().create_task(catalog.FREE.refresh(self._client))
            except RuntimeError:
                pass

    def feedback(self, model_id: str, task_type: str, complexity: float, good: bool, source: str = "thumbs") -> None:
        self.store.feedback(model_id=model_id, task_type=task_type, complexity=complexity, good=good, source=source)
        if not good and model_id:
            # Don't serve the same cached answer again.
            self.store.cache_drop_model(model_id)

    async def run(self, req: RunRequest, on_delta: Callable[[int, str], None] | None = None) -> RunResult:
        """on_delta(attempt_number, text) streams the answer as it's written (attempt changes on escalation)."""
        t_start = time.perf_counter()
        rid = uuid.uuid4().hex[:12]
        self.refresh_free_quota()
        profile, decision, skills, ctx, ctx_info, routing_ms = self.plan(req)
        if profile.confidence < float(self.registry.routing.get("llm_classifier_below_confidence", 0.0)):
            profile = await self._maybe_llm_classify(req, profile)
            decision = route(self.registry, self.store, profile, prefer=req.prefer,
                             quality_override=req.quality_target, max_cost_usd=req.max_cost_usd,
                             force_model=req.force_model, allow_free=self._allow_free(req),
                         exclude=req.exclude, private=req.private)
            skills = self.skills.select(req.instruction, profile)

        result = RunResult(rid, "", None, False, profile.as_dict(), decision.as_dict(),
                           [s.name for s in skills], ctx_info, routing_ms=routing_ms)
        if req.dry_run:
            result.total_ms = (time.perf_counter() - t_start) * 1000
            return result

        system = BASE_SYSTEM + (f"\n\n{req.system_extra}" if req.system_extra else "") + \
            ("\n\n" + "\n\n".join(s.body for s in skills) if skills else "")
        user = req.instruction if not ctx else f"{req.instruction}\n\n<context>\n{ctx}\n</context>"

        key = Store.cache_key(req.instruction, ctx, req.tests or "", profile.task_type,
                              req.system_extra, req.prefer, str(req.quality_target or ""),
                              str(req.has_images))
        if req.use_cache and not req.force_model:
            hit = self.store.cache_get(key, self.cache_ttl)
            if hit:
                result.output, result.model, result.passed, result.cached = hit["output"], hit["model"], True, True
                result.total_ms = (time.perf_counter() - t_start) * 1000
                return result

        max_out = min(int(self.registry.defaults.get("max_output_tokens", 4096)),
                      int(_OUT_BUDGET.get(profile.task_type, 1000) * (0.8 + 1.6 * profile.complexity)))
        ladder = [decision.primary] + trim_ladder(decision.ladder, self.max_attempts - 1)
        # The effective cap is whichever binds first: this request's cap, or the daily budget.
        budget_cap = req.max_cost_usd if req.max_cost_usd is not None else math.inf
        budget_cap = min(budget_cap, self._daily_budget_left())
        spent = 0.0
        feedback = ""
        best: tuple[float, str, str] | None = None   # (score, text, model)
        empty_retry: dict[str, bool] = {}
        blocked_by_budget = False

        for i, cand in enumerate(ladder):
            m: ModelSpec = cand.model
            if m.id != req.force_model and spent + cand.est_cost > budget_cap:
                blocked_by_budget = True
                continue                                 # hard budget: try a cheaper rung, else stop
            free = bool(m.extra.get("free"))
            if free and not m.extra.get("quota") and not catalog.FREE.available() and m.id != req.force_model:
                continue
            res = None
            if m.extra.get("quota"):
                ok, _why, res = quota.LEDGER.begin(m, profile.est_input_tokens + max_out // 2)
                if not ok:
                    # Forced or last-choice model: still accounted — don't bypass the ledger.
                    result.attempts.append(Attempt(m.id, False, 0.0, [], 0, 0, 0.0, 0.0,
                                                   error=f"free-tier limit reached: {_why}"))
                    continue                             # limit reached between routing and send
            prompt = user if not feedback else f"{user}\n\n{feedback}"
            prov = self.providers[m.provider]
            kw = dict(json_mode="json" in profile.needs, web="web" in profile.needs)
            try:
                if on_delta is not None:
                    n = len(result.attempts) + 1
                    comp = await prov.stream(m, system, prompt, max_out, lambda t, n=n: on_delta(n, t), **kw)
                else:
                    comp = await prov.complete(m, system, prompt, max_out, **kw)
                quota.LEDGER.settle(res, m, comp.tokens_in + comp.tokens_out, comp.headers)
                res = None
                self.registry.breaker.record(m.id, True)
                if free and not m.extra.get("quota"):
                    catalog.FREE.note_used()
            except (ProviderError, httpx.HTTPError) as e:
                quota.LEDGER.abandon(res)
                if quota.after_error(m, e):
                    pass                                     # free-tier refusal: paused, move on
                elif free and "429" in str(e):
                    catalog.FREE.note_rate_limited(str(e))     # not the model's fault: don't trip the breaker
                else:
                    self.registry.breaker.record(m.id, False)
                result.attempts.append(Attempt(m.id, False, 0.0, [], 0, 0, 0.0, 0.0,
                                               error=secrets.redact(str(e))[:300]))
                if i == len(ladder) - 1 and free and catalog.paid_allowed():
                    # The last option was a free model that refused: the person allows paid models,
                    # so try once more without free ones. Never happens with paid models off.
                    try:
                        retry = route(self.registry, self.store, profile, prefer=req.prefer,
                                      quality_override=req.quality_target, max_cost_usd=req.max_cost_usd,
                                      allow_free=False, exclude=req.exclude, private=req.private)
                        if retry.primary.model.id not in {c.model.id for c in ladder}:
                            ladder.append(retry.primary)
                    except RuntimeError:
                        pass
                continue

            if not comp.text.strip() and comp.tokens_out and not empty_retry.get(m.id):
                # A "thinking" model spent its whole allowance reasoning and returned no answer.
                # That's not a quality failure: give the same model more room once (free models
                # cost nothing) before moving on.
                empty_retry[m.id] = True
                ladder.insert(i + 1, cand)
                max_out = min(max(max_out * 3, 4000), 16000)
                result.attempts.append(Attempt(m.id, False, 0.0, ["no answer: it used its whole reply "
                                                                   "allowance on hidden reasoning"],
                                               comp.tokens_in, comp.tokens_out,
                                               m.cost(comp.tokens_in, comp.tokens_out, comp.cached_tokens),
                                               comp.latency_ms))
                spent += result.attempts[-1].cost_usd
                self.store.add_spend(result.attempts[-1].cost_usd)
                continue

            cost = m.cost(comp.tokens_in, comp.tokens_out, comp.cached_tokens)
            spent += cost
            self.store.add_spend(cost)
            # Checks can run pytest (subprocess): keep them off the event loop.
            chk = await asyncio.to_thread(ck.run_checks, profile, req.instruction, comp.text,
                                          comp.stop_reason, req.tests, self.run_code)
            att = Attempt(m.id, chk.passed, chk.score, chk.reasons, comp.tokens_in, comp.tokens_out,
                          cost, comp.latency_ms)
            result.attempts.append(att)

            if chk.passed and self._should_review(req, profile, m):
                left = (budget_cap - spent) if math.isfinite(budget_cap) else None
                ok, issues, rev = await self._review(req, m, comp.text, budget_left=left)
                if rev:
                    result.attempts.append(rev)
                    spent += rev.cost_usd
                    self.store.add_spend(rev.cost_usd)
                if not ok:
                    chk.passed = False
                    chk.reasons = ["cross-review: " + x for x in issues] or ["cross-review rejected"]
                    att.passed, att.reasons = False, chk.reasons

            if m.provider != "offline":
                self.store.record(request_id=rid, model_id=m.id, task_type=profile.task_type,
                                  complexity=profile.complexity, passed=chk.passed, cost_usd=cost,
                                  latency_ms=comp.latency_ms, tokens_in=comp.tokens_in,
                                  tokens_out=comp.tokens_out, attempt=i + 1, reasons=chk.reasons)
            if best is None or chk.score > best[0]:
                best = (chk.score, comp.text, m.id)
            if chk.passed:
                result.output, result.model, result.passed = comp.text, m.id, True
                if req.use_cache and m.provider != "offline":
                    self.store.cache_put(key, {"output": comp.text, "model": m.id})
                break
            # Escalate: tell the next model exactly what failed; include the
            # previous attempt only for code (it's usually close), capped.
            if "cut off" in " ".join(chk.reasons):
                max_out = min(max_out * 2, 16000)
            prev = comp.text[:6000] if profile.task_type == "coding" else ""
            feedback = ("A previous attempt failed these checks:\n- " + "\n- ".join(r[:800] for r in chk.reasons)
                        + (f"\n\nPrevious attempt (fix it, don't start over unless needed):\n{prev}" if prev else ""))

        if not result.passed and best:
            result.output, result.model = best[1], best[2]
        elif not result.attempts and blocked_by_budget:
            cap = float(catalog.get_settings().get("daily_budget_usd") or 0)
            result.output = (f"Stopped before spending: today's ${cap:.2f} budget is used up. "
                             "Free models may still work; raise the daily cap in Connections."
                             if cap and budget_cap <= 0 else
                             f"Stopped before spending: no suitable model fits the remaining "
                             f"${budget_cap:.4f} budget.")
        result.total_ms = (time.perf_counter() - t_start) * 1000
        return result

    # ---------------------------------------------------------------- review
    def _should_review(self, req: RunRequest, profile: TaskProfile, m: ModelSpec) -> bool:
        mode = req.cross_review or self.registry.routing.get("cross_review", "auto")
        if mode == "off" or m.provider == "offline":
            return False
        if mode == "always":
            return True
        # auto: only for hard work that deterministic checks can't fully verify
        verified_by_tests = profile.task_type == "coding" and ("code_exec" in profile.needs or req.tests)
        return profile.complexity >= float(self.registry.routing.get("cross_review_complexity", 0.75)) \
            and not verified_by_tests

    async def _review(self, req: RunRequest, author: ModelSpec, text: str,
                      budget_left: float | None = None) -> tuple[bool, list[str], Attempt | None]:
        pool = self._secondary_pool(req, exclude_id=author.id)
        if not pool:
            return True, [], None
        other = [m for m in pool if m.provider != author.provider] or pool
        reviewer = min(other, key=lambda m: (m.price_in + m.price_out, -m.tier))
        if budget_left is not None and reviewer.cost(4200, 300) > budget_left:
            return True, [], None                            # can't afford a review within the cap
        msg = f"TASK:\n{req.instruction[:4000]}\n\nRESPONSE:\n{text[:12000]}"
        c: Completion | None = await self._secondary_call(reviewer, ck.REVIEW_SYSTEM, msg, 300, json_mode=True)
        if c is None:
            return True, [], Attempt(reviewer.id, True, 0, [], 0, 0, 0, 0, kind="review",
                                     error="review call unavailable")
        score, issues = ck.parse_review(c.text)
        cost = reviewer.cost(c.tokens_in, c.tokens_out)
        return score >= 7, issues, Attempt(reviewer.id, score >= 7, score / 10, issues, c.tokens_in,
                                           c.tokens_out, cost, c.latency_ms, kind="review")

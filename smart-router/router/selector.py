# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Model selection: cheapest/fastest model expected to clear the quality bar,
plus an escalation ladder of stronger models. Pure in-memory math.

Author: Akshay Dubey
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import catalog, quota
from .classifier import TaskProfile
from .registry import ModelSpec, Registry
from .store import Store

HARD_NEEDS = {"vision", "long_context"}

# Rough output length by task type (tokens) before telemetry takes over.
_BASE_OUT = {"coding": 900, "research": 1100, "writing": 700, "document": 900, "data": 700, "chat": 250}


@dataclass
class Candidate:
    model: ModelSpec
    quality: float
    est_cost: float
    est_latency_s: float
    score: float
    clears_bar: bool


@dataclass
class RouteDecision:
    target: float
    primary: Candidate
    ladder: list[Candidate] = field(default_factory=list)   # escalation order, after primary
    considered: list[Candidate] = field(default_factory=list)
    reason: str = ""
    profile: TaskProfile | None = None
    free_skipped: str = ""                 # why free models were left out, if they were

    def as_dict(self) -> dict:
        def c(x: Candidate) -> dict:
            return {"model": x.model.id, "expected_quality": round(x.quality, 3),
                    "est_cost_usd": round(x.est_cost, 6), "est_latency_s": round(x.est_latency_s, 2),
                    "clears_bar": x.clears_bar}
        return {"quality_target": round(self.target, 3), "primary": c(self.primary),
                "escalation_ladder": [c(x) for x in self.ladder], "reason": self.reason,
                "why": self.why()}

    def why(self, limit: int = 6) -> dict:
        """Plain-language account of the choice, for the "Why this model?" panel."""
        p, prim = self.profile, self.primary
        cx = p.complexity if p else 0.5
        level = "easy" if cx < 0.35 else ("medium" if cx < 0.6 else "hard")
        able = [c for c in self.considered if c.clears_bar]
        strongest = max(self.considered, key=lambda c: (c.quality, -c.est_cost))
        ranked = sorted(self.considered, key=lambda c: (not c.clears_bar, -c.score))
        shown = ranked[:limit]
        if prim not in shown:
            shown = [prim] + shown[:limit - 1]
        if strongest not in shown:
            shown = shown[:limit - 1] + [strongest]
        free = bool(prim.model.extra.get("free"))
        if self.reason == "forced by caller":
            because = "you chose this model."
        elif not able:
            because = "no model was expected to reach the bar, so the strongest available one was used."
        elif free:
            because = "it's free and expected to be good enough for this."
        elif prim is strongest:
            because = "only the strongest models are expected to do this well."
        else:
            because = "it's the lowest-cost model expected to do this well."
        return {
            "task_type": p.task_type if p else "", "difficulty": round(cx, 2), "level": level,
            "quality_needed": round(self.target, 2), "models_considered": len(self.considered),
            "models_able": len(able), "because": because, "free_skipped": self.free_skipped,
            "strongest": {"id": strongest.model.id, "name": strongest.model.extra.get("name", strongest.model.id),
                          "est_cost_usd": round(strongest.est_cost, 6)},
            "est_saving_usd": round(max(0.0, strongest.est_cost - prim.est_cost), 6),
            "options": [{"id": c.model.id, "name": c.model.extra.get("name", c.model.id),
                         "free": bool(c.model.extra.get("free")), "expected_quality": round(c.quality, 2),
                         "est_cost_usd": round(c.est_cost, 6), "est_seconds": round(c.est_latency_s, 1),
                         "able": c.clears_bar, "chosen": c is prim} for c in shown],
        }


def quality_target(reg: Registry, profile: TaskProfile, override: float | None = None) -> float:
    if override is not None:
        return override
    base = reg.quality_targets.get(profile.task_type, 0.75)
    # Harder work deserves a higher bar; trivial work a lower one.
    return max(0.5, min(0.97, base + 0.15 * (profile.complexity - 0.5)))


def route(reg: Registry, store: Store, profile: TaskProfile, *, prefer: str = "balanced",
          quality_override: float | None = None, max_cost_usd: float | None = None,
          force_model: str | None = None, allow_free: bool = True,
          exclude: tuple[str, ...] = (), private: bool = False,
          require: frozenset[str] | set[str] = frozenset(), est_output: int | None = None) -> RouteDecision:
    """private: skip free providers that may train on prompts (used for code).
    require: capabilities a model must have (e.g. {"tools"} for coding tools)."""
    target = quality_target(reg, profile, quality_override)
    w_cost = reg.weights.get("cost_per_usd", 40.0)
    w_lat = reg.weights.get("latency_per_s", 0.02)
    if prefer == "cheap":
        w_cost *= 4
    elif prefer == "fast":
        w_lat *= 6
    elif prefer == "best":
        target = max(target, 0.95)

    hard = profile.needs & HARD_NEEDS
    cands: list[Candidate] = []
    free_ok = catalog.FREE.available()
    free_skipped = ""
    require = frozenset(require)
    ledger = quota.LEDGER
    for m in reg.usable():
        if m.id in exclude and m.id != force_model:
            continue
        if require - m.capabilities and m.id != force_model:
            continue
        if private and m.extra.get("free") and m.extra.get("trains") and m.id != force_model:
            free_skipped = free_skipped or "free providers that may train on your prompts aren't used for code"
            continue
        if m.extra.get("quota") and m.id != force_model:
            ok, why = ledger.allow(m, profile.est_input_tokens + (est_output or 0))
            if not ok:
                free_skipped = free_skipped or why
                continue
        if m.extra.get("free") and m.id != force_model and \
                (not allow_free or (not free_ok and not m.extra.get("quota"))):
            snap = catalog.FREE.snapshot() if allow_free else {}
            free_skipped = ("free models are switched off for this kind of work" if not allow_free else
                            "today's free requests are used up" if snap.get("pause_reason") == "daily"
                            or snap.get("remaining") == 0 else "free models are rate-limited for a minute")
            continue
        if hard - m.capabilities:
            continue
        if profile.est_input_tokens > m.context_window * 0.9:
            continue
        q = store.expected_quality(m.id, profile.task_type, profile.complexity,
                                   m.prior.get(profile.task_type, 0.6))
        if "web" in profile.needs and "web" in m.capabilities:
            q = min(1.0, q + 0.04)
        out_tok = store.observed_out_tokens(m.id, profile.task_type, profile.complexity) or \
            _BASE_OUT.get(profile.task_type, 600) * (0.6 + profile.complexity)
        cost = m.cost(profile.est_input_tokens + 400, int(out_tok))
        lat_ms = store.observed_latency_ms(m.id, profile.task_type, profile.complexity) or \
            m.latency_ms_p50 * (0.7 + profile.complexity)
        lat = lat_ms / 1000
        clears = q >= target and (max_cost_usd is None or cost <= max_cost_usd)
        # Among models that clear the bar, lower penalty wins; small bonus for quality headroom.
        score = -(cost * w_cost + lat * w_lat) + 0.5 * (q - target)
        # Free models: for easy, everyday requests a $0 answer beats a slightly faster paid one.
        if m.extra.get("free") and profile.complexity < 0.45 and prefer != "best":
            score += 0.25 if prefer in ("balanced", "cheap") else 0.05
        cands.append(Candidate(m, q, cost, lat, score, clears))

    if not cands:
        raise RuntimeError("No usable model satisfies this task's hard requirements "
                           f"({sorted(hard) or 'none'}). Enable a model in config/models.yaml.")

    if force_model:
        forced = [c for c in cands if c.model.id == force_model]
        if not forced:
            raise RuntimeError(f"Model '{force_model}' is not usable (disabled, no key, or unavailable).")
        primary = forced[0]
        reason = "forced by caller"
    else:
        passing = sorted((c for c in cands if c.clears_bar), key=lambda c: c.score, reverse=True)
        if passing:
            primary = passing[0]
            reason = (f"cheapest/fastest model expected to reach quality {target:.2f} "
                      f"for {profile.task_type} (complexity {profile.complexity:.2f})")
        else:
            affordable = [c for c in cands if max_cost_usd is None or c.est_cost <= max_cost_usd]
            if affordable:
                primary = max(affordable, key=lambda c: (c.quality, c.score))
                reason = "no model expected to clear the bar; chose the strongest available within budget"
            else:
                primary = min(cands, key=lambda c: c.est_cost)
                reason = "nothing fits the budget; chose the cheapest model"

    # Escalation ladder: step up one notch at a time (next-stronger first), so a
    # near-miss doesn't jump straight to the most expensive model. A different
    # provider at similar strength counts as a step (independent failure modes).
    stronger = [c for c in cands if c.model.id != primary.model.id and
                (c.quality > primary.quality + 0.01 or
                 (c.model.provider != primary.model.provider and c.quality >= primary.quality - 0.02))]
    if max_cost_usd is not None:
        stronger = [c for c in stronger if c.est_cost <= max_cost_usd]
    ladder = sorted(stronger, key=lambda c: (round(c.quality, 2), c.est_cost))
    return RouteDecision(target, primary, ladder, cands, reason, profile, free_skipped)


def trim_ladder(ladder: list[Candidate], slots: int) -> list[Candidate]:
    """Keep the next step(s) up but always end with the strongest option (no duplicates)."""
    if slots <= 0:
        return []
    if len(ladder) <= slots:
        return ladder
    head = ladder[: slots - 1]
    top = max(ladder, key=lambda c: c.quality)
    if any(c.model.id == top.model.id for c in head):
        return head
    return head + [top]

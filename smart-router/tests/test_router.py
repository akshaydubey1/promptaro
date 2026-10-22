# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Tests run fully offline: model calls go to a scripted fake provider."""
from __future__ import annotations

import asyncio
import math
import statistics
import time
from pathlib import Path

import pytest
import yaml

from router import catalog
from router.classifier import classify
from router.context import fit_context
from router.engine import Router, RunRequest
from router.providers import Completion, Provider, ProviderError

ROOT = Path(__file__).resolve().parents[1]

GOOD_PY = "```python\n# calc.py\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
BAD_PY = "```python\n# calc.py\ndef add(a, b)\n    return a + b\n```"
WRONG_PY = "```python\n# calc.py\ndef add(a: int, b: int) -> int:\n    return a - b\n```"
TESTS = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


class Scripted(Provider):
    """Returns canned replies per model id; records calls."""

    def __init__(self, replies: dict[str, list]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[str] = []

    async def complete(self, spec, system, user, max_tokens, **kw):
        self.calls.append(spec.id)
        q = self.replies.get(spec.id) or ["ok"]
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        return Completion(r, 500, 200, 5.0, "end_turn")


@pytest.fixture
def make_router(tmp_path, monkeypatch, test_root):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def _make(replies: dict[str, list]):
        fake = Scripted(replies)
        r = Router(test_root, db_path=tmp_path / "t.db", providers={"anthropic": fake, "openai": fake,
                                                                "openai_compatible": fake, "offline": fake})
        return r, fake
    return _make


def test_refresh_catalog_loads_fresh_models(tmp_path, test_root, monkeypatch):
    """Startup builds cache-only; refresh_catalog() swaps in a registry built from the live list."""
    cfg = yaml.safe_load((test_root / "config" / "models.yaml").read_text())
    cfg["catalog"]["enabled"] = True
    cfg["catalog"]["vendors"] = ["acme"]
    (test_root / "config" / "models.yaml").write_text(yaml.safe_dump(cfg))
    fresh = {"id": "acme/fresh-9", "name": "acme/fresh-9", "created": int(time.time()),
             "context_length": 200000, "pricing": {"prompt": "0.000001", "completion": "0.000005"},
             "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
             "supported_parameters": ["tools"]}
    monkeypatch.setattr(catalog, "fetch_openrouter_models", lambda *a, **k: [fresh])
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")

    r = Router(test_root, db_path=tmp_path / "t.db", refresh_catalog=False)
    assert "acme/fresh-9" not in r.registry.models           # cache-only build: nothing fresh yet
    old = r.registry
    asyncio.run(r.refresh_catalog())
    assert r.registry is not old and "acme/fresh-9" in r.registry.models


def run(r, **kw):
    return asyncio.run(r.run(RunRequest(**kw)))


# ---------------------------------------------------------------- classify
@pytest.mark.parametrize("text,expected", [
    ("Write a Python function to parse a CSV header and add pytest tests", "coding"),
    ("Fix the bug in this terraform module for GKE", "coding"),
    ("Research the latest trends in GPU pricing and compare sources", "research"),
    ("Draft a short email to my manager about Friday's release", "writing"),
    ("Extract the totals from the attached PDF invoice", "document"),
    ("hey, how's it going?", "chat"),
])
def test_classifier_types(text, expected):
    assert classify(text).task_type == expected


def test_complexity_orders_sensibly():
    easy = classify("quick one-line rename of a variable in python")
    hard = classify("Design a production, distributed, scalable event pipeline in Go with security "
                    "trade-offs, migration plan and edge cases")
    assert hard.complexity > easy.complexity + 0.3


def test_generated_code_runs_only_in_container_by_default(monkeypatch):
    """auto = container only; an explicit true/false in config wins."""
    from router import engine
    monkeypatch.setattr(engine, "_in_container", lambda: False)
    assert engine._run_code_setting("auto") is False    # the shipped config value
    assert engine._run_code_setting(None) is False      # unset behaves like auto
    assert engine._run_code_setting(True) is True       # user explicitly enabled it
    assert engine._run_code_setting(False) is False
    monkeypatch.setattr(engine, "_in_container", lambda: True)
    assert engine._run_code_setting("auto") is True


# ----------------------------------------------------------------- routing
def test_simple_task_goes_to_small_model(make_router):
    r, _ = make_router({})
    res = run(r, instruction="hi, what's a good name for a cat?", dry_run=True)
    assert res.route["primary"]["model"] == "claude-haiku"


def test_hard_coding_goes_to_stronger_model(make_router):
    r, _ = make_router({})
    res = run(r, instruction="Architect a production-grade distributed rate limiter in Python with "
                             "concurrency safety, edge cases and pytest tests", dry_run=True)
    assert res.route["primary"]["model"] in ("claude-sonnet", "claude-opus")


def test_prefer_best_picks_frontier(make_router):
    r, _ = make_router({})
    res = run(r, instruction="summarize this paragraph", prefer="best", dry_run=True)
    assert res.route["primary"]["model"] == "claude-opus"


# -------------------------------------------------------- checks/escalation
def test_escalates_on_syntax_error(make_router):
    r, fake = make_router({"claude-haiku": [BAD_PY], "claude-sonnet": [GOOD_PY]})
    res = run(r, instruction="write a python function add(a, b)", use_cache=False)
    assert res.passed and res.model == "claude-sonnet"
    assert fake.calls == ["claude-haiku", "claude-sonnet"]
    assert "syntax" in res.attempts[0].reasons[0]


def test_runs_user_tests_and_escalates_on_failure(make_router):
    r, fake = make_router({"claude-haiku": [WRONG_PY], "claude-sonnet": [GOOD_PY]})
    r.run_code = True                  # the "user explicitly enabled it" path
    res = run(r, instruction="write a python function add(a, b)", tests=TESTS, use_cache=False)
    assert res.passed and res.model == "claude-sonnet"
    assert res.attempts[0].reasons[0].startswith("tests failed")


def test_json_check(make_router):
    r, _ = make_router({"claude-haiku": ["not json at all"], "claude-sonnet": ['```json\n{"a": 1}\n```']})
    res = run(r, instruction="return json with field a set to 1", use_cache=False)
    assert res.passed and res.model == "claude-sonnet"


def test_provider_error_falls_through(make_router):
    r, _ = make_router({"claude-haiku": [ProviderError("503")], "claude-sonnet": ["Paris."]})
    res = run(r, instruction="what's the capital of France? quick", use_cache=False)
    assert res.passed and res.model == "claude-sonnet"
    assert res.attempts[0].error


def test_budget_stops_escalation(make_router):
    r, fake = make_router({"claude-haiku": [BAD_PY]})
    res = run(r, instruction="write a python function add(a, b)", max_cost_usd=0.005, use_cache=False)
    assert not res.passed
    assert fake.calls == ["claude-haiku"]


def test_spend_is_recorded_and_budget_left_is_open_without_cap(make_router):
    r, _ = make_router({"claude-haiku": ["Hello!"]})
    run(r, instruction="say hello", use_cache=False)
    assert r.store.spend_today() > 0
    assert r._daily_budget_left() == math.inf          # no cap configured


def test_daily_budget_blocks_paid_models_when_used_up(make_router, monkeypatch):
    from router import catalog
    r, fake = make_router({"claude-haiku": ["Hello!"]})
    r.store.add_spend(1.0)
    monkeypatch.setattr(catalog, "get_settings",
                        lambda: {"daily_budget_usd": 0.5, "free_models": "chat", "gateway_spend": "free", "allow_paid": True})
    res = run(r, instruction="say hello", use_cache=False)
    assert not res.passed and "budget" in res.output
    assert fake.calls == []                            # nothing paid was even attempted


# ------------------------------------------------------------ cache, learning
def test_cache_avoids_second_call(make_router):
    r, fake = make_router({"claude-haiku": ["Hello!"]})
    a = run(r, instruction="say hello")
    b = run(r, instruction="say   Hello")          # whitespace/case-normalised
    assert a.passed and b.cached and fake.calls == ["claude-haiku"]


def test_learns_from_failures(make_router):
    r, _ = make_router({})
    instr = "write a python function to merge two sorted lists"
    before = run(r, instruction=instr, dry_run=True).route["primary"]["model"]
    assert before == "claude-haiku"
    prof = classify(instr)
    for _ in range(12):
        r.store.record(request_id="x", model_id="claude-haiku", task_type="coding", complexity=prof.complexity,
                       passed=False, cost_usd=0.001, latency_ms=900, tokens_in=500, tokens_out=300,
                       attempt=1, reasons=["tests failed"])
    after = run(r, instruction=instr, dry_run=True).route["primary"]["model"]
    assert after != "claude-haiku"


# ---------------------------------------------------------------- context
def test_context_trimming_keeps_relevant_chunk():
    filler = "\n\n".join(f"Paragraph {i} about gardening, tomatoes and compost." for i in range(3000))
    needle = "The Kubernetes ingress timeout is configured in nginx annotations."
    ctx = filler[: len(filler) // 2] + "\n\n" + needle + "\n\n" + filler[len(filler) // 2:]
    out, info = fit_context("How is the kubernetes ingress timeout configured?", ctx, 2000)
    assert info["trimmed"] and info["context_tokens_sent"] <= 2000
    assert needle in out


# ---------------------------------------------------------------- offline
def test_runs_with_no_keys(tmp_path, monkeypatch, test_root):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = Router(test_root, db_path=tmp_path / "o.db")
    res = asyncio.run(r.run(RunRequest("hello")))
    asyncio.run(r.aclose())
    assert res.model == "offline" and not res.passed and "[offline]" in res.output


# ------------------------------------------------------------ routing speed
def test_routing_under_100ms(make_router):
    r, _ = make_router({})
    prompts = ["write a python function add(a, b)", "draft an email to the team",
               "research current kubernetes autoscaling options with sources", "hello"] * 50
    ctx = "some context line about services and deployments\n\n" * 400
    times = []
    for p in prompts:
        t = time.perf_counter()
        r.plan(RunRequest(p, ctx))
        times.append((time.perf_counter() - t) * 1000)
    p99 = sorted(times)[int(len(times) * 0.99) - 1]
    assert p99 < 100, f"p99 routing {p99:.2f}ms; median {statistics.median(times):.2f}ms"


# -------------------------------------------------------------- quota accounting
def test_forced_model_still_uses_quota_ledger(make_router, monkeypatch):
    """A forced free-tier model must still reserve/settle — selection is forced, accounting isn't."""
    from router import quota
    from router.registry import ModelSpec
    r, fake = make_router({"free-q": [GOOD_PY]})
    r.registry.models["free-q"] = ModelSpec(
        id="free-q", provider="anthropic", model="q", tier=1, context_window=200000,
        capabilities={"json"}, price_in=0, price_out=0, latency_ms_p50=100, prior={}, api_key_env="",
        extra={"quota": "groq", "free": True, "limits": {"rpm": 30, "rpd": 100, "tpm": 8000}})
    calls = []
    orig = quota.LEDGER.begin
    monkeypatch.setattr(quota.LEDGER, "begin",
                        lambda spec, est=0: (calls.append(spec.id), orig(spec, est))[1])
    res = run(r, instruction="Write a Python function `add` that sums two ints", force_model="free-q")
    assert "free-q" in calls


def test_secondary_calls_use_quota_ledger(make_router, monkeypatch):
    """Review/classify helper calls count against the ledger like primary calls."""
    from router import quota
    from router.registry import ModelSpec
    r, fake = make_router({"free-q": [GOOD_PY]})
    r.registry.models["free-q"] = ModelSpec(
        id="free-q", provider="anthropic", model="q", tier=1, context_window=200000,
        capabilities={"json"}, price_in=0, price_out=0, latency_ms_p50=100, prior={}, api_key_env="",
        extra={"quota": "groq", "free": True, "limits": {"rpm": 30, "rpd": 100, "tpm": 8000}})
    c = asyncio.run(r._secondary_call(r.registry.models["free-q"], "sys", "u", 10))
    assert c is not None and sum(v[0] for v in quota.LEDGER.daily.values()) == 1

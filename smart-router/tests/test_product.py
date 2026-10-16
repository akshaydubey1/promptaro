# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Every model family, free-request allowance, explanations, learning from feedback, streaming.
Author: Akshay Dubey"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import yaml

from router import catalog, secrets
from router.classifier import classify
from router.engine import Router, RunRequest
from router.providers import AnthropicProvider, OpenAIProvider, Provider, ProviderError, Completion
from router.registry import Registry
from router.selector import route
from router.store import Store

NOW = int(time.time())


def m(mid, p_in, p_out, created=NOW - 10, ctx=200000):
    return {"id": mid, "name": mid, "created": created, "context_length": ctx,
            "pricing": {"prompt": str(p_in / 1e6), "completion": str(p_out / 1e6)},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools", "response_format"]}


MARKET = [
    m("anthropic/claude-small", 1, 5), m("anthropic/claude-mid", 3, 15), m("anthropic/claude-big", 5, 25),
    m("openai/gpt-mini", 0.25, 2), m("openai/gpt-max", 10, 40),
    m("deepseek/deepseek-v9", 0.3, 1.2), m("deepseek/deepseek-r9", 0.5, 2.2),
    m("x-ai/grok-9", 3, 15), m("mistralai/mistral-large-9", 2, 6), m("qwen/qwen9-max", 1.2, 6),
    m("meta-llama/llama-9-405b", 0.8, 0.8), m("brandnew/mystery-1", 0.01, 0.01),
    m("openrouter/auto", 1, 1),
    m("meta-llama/llama-free:free", 0, 0, ctx=128000),
]


def _registry(tmp_path, vendors="all"):
    catalog._write_cache("openrouter_models.json", MARKET)
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "models.yaml").read_text())
    cfg["catalog"]["vendors"] = vendors
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return Registry(p), Store(tmp_path / "s.db")


# -------------------------------------------------------------- every family
def test_all_vendors_covers_the_market(tmp_path):
    reg, _ = _registry(tmp_path)
    families = {s.extra["vendor"] for s in reg.usable()}
    assert {"anthropic", "openai", "deepseek", "x-ai", "mistralai", "qwen", "meta-llama", "brandnew"} <= families
    assert "openrouter" not in families


def test_unknown_cheap_family_starts_with_less_trust(tmp_path):
    reg, _ = _registry(tmp_path)
    mystery, haiku = reg.models["brandnew/mystery-1"], reg.models["anthropic/claude-small"]
    assert mystery.prior["coding"] < haiku.prior["coding"]
    # a medium task is not handed to an unknown $0.01 model just because it's cheapest
    d = route(reg, Store(tmp_path / "x.db"), classify("Refactor this module to use dependency injection and "
                                                       "add unit tests for the edge cases"))
    assert d.primary.model.id != "brandnew/mystery-1"


def test_price_caps_size_class(tmp_path):
    reg, _ = _registry(tmp_path)
    assert reg.models["deepseek/deepseek-r9"].tier == 1          # its "largest" is still cheap
    assert reg.models["openai/gpt-max"].tier == 3


def test_routing_stays_fast_with_every_family(tmp_path):
    reg, st = _registry(tmp_path)
    prof = classify("write a python function that merges two sorted lists")
    t0 = time.perf_counter()
    for _ in range(50):
        route(reg, st, prof)
    assert (time.perf_counter() - t0) / 50 < 0.01                # well under the 100 ms budget


# ---------------------------------------------------------- free allowance
def test_free_quota_reads_openrouter_and_counts_down():
    q = catalog.FreeQuota()
    q.apply({"data": {"is_free_tier": True, "free_model_daily_requests": {"used": 48, "limit": 50, "remaining": 2}}})
    assert q.snapshot()["remaining"] == 2 and q.available()
    q.note_used()
    q.note_used()
    assert q.snapshot()["remaining"] == 0 and not q.available()


def test_free_quota_without_detail_uses_plan_limits():
    q = catalog.FreeQuota()
    q.apply({"data": {"is_free_tier": False}})
    assert q.limit == 1000 and q.remaining == 1000


def test_rate_limit_pauses_free_models():
    q = catalog.FreeQuota()
    q.note_rate_limited("429 Rate limit exceeded: free-models-per-min")
    s = q.snapshot()
    assert not s["available"] and 0 < s["paused_for_s"] <= 65 and s["pause_reason"] == "minute"
    q2 = catalog.FreeQuota()
    q2.note_rate_limited("429 Rate limit exceeded: free-models-per-day")
    assert q2.snapshot()["pause_reason"] == "daily" and q2.snapshot()["remaining"] == 0


def test_router_skips_free_models_when_allowance_is_used_up(tmp_path):
    reg, st = _registry(tmp_path)
    easy = classify("tell me a joke")
    assert route(reg, st, easy).primary.model.extra["free"] is True
    catalog.FREE.apply({"data": {"free_model_daily_requests": {"used": 50, "limit": 50, "remaining": 0}}})
    d = route(reg, st, easy)
    assert d.primary.model.extra["free"] is False
    assert "used up" in d.why()["free_skipped"]


# ------------------------------------------------------------ explanations
def test_why_explains_the_choice(tmp_path):
    reg, st = _registry(tmp_path)
    w = route(reg, st, classify("tell me a joke")).why()
    assert w["level"] == "easy" and w["because"].startswith("it's free")
    assert any(o["chosen"] for o in w["options"]) and w["models_considered"] > 5
    assert w["est_saving_usd"] > 0 and w["strongest"]["id"]
    hard = route(reg, st, classify("Architect a production-grade distributed rate limiter with security "
                                   "trade-offs and concurrency edge cases, with a migration plan")).why()
    assert hard["level"] == "hard" and hard["quality_needed"] > w["quality_needed"]


# ------------------------------------------------------------------ learning
def test_thumbs_down_moves_routing_away_and_up_brings_it_back(tmp_path):
    reg, st = _registry(tmp_path, ["anthropic", "openai"])
    catalog.set_settings(free_models="off")
    reg, st = _registry(tmp_path, ["anthropic", "openai"])
    q = classify("write a short python function to reverse a string")
    first = route(reg, st, q, allow_free=False).primary.model.id
    for _ in range(4):
        st.feedback(model_id=first, task_type=q.task_type, complexity=q.complexity, good=False)
    second = route(reg, st, q, allow_free=False).primary.model.id
    assert second != first
    st2 = Store(tmp_path / "s.db")                                 # learning survives a restart
    assert route(reg, st2, q, allow_free=False).primary.model.id == second
    for _ in range(40):                                           # consistent praise wins it back
        st2.feedback(model_id=first, task_type=q.task_type, complexity=q.complexity, good=True)
    assert route(reg, st2, q, allow_free=False).primary.model.id == first


# ----------------------------------------------------------------- streaming
def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def test_openai_compatible_stream():
    body = _sse({"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    from router.registry import ModelSpec
    s = ModelSpec(id="x/y", provider="openai_compatible", model="y", tier=1, context_window=1000,
                  capabilities=set(), price_in=1, price_out=1, latency_ms_p50=100, prior={}, base_url="https://example.test/v1")
    got = []
    c = asyncio.run(OpenAIProvider(client, compatible=True).stream(s, "sys", "hi", 50, got.append))
    assert got == ["Hel", "lo"] and c.text == "Hello" and (c.tokens_in, c.tokens_out) == (12, 2)


def test_anthropic_stream():
    body = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 9, "cache_read_input_tokens": 3}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi "}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "there"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    from router.registry import ModelSpec
    s = ModelSpec(id="anthropic/x", provider="anthropic", model="x", tier=1, context_window=1000,
                  capabilities=set(), price_in=1, price_out=1, latency_ms_p50=100, prior={}, api_key_env="ANTHROPIC_API_KEY")
    secrets.set("ANTHROPIC_API_KEY", "k")
    got = []
    c = asyncio.run(AnthropicProvider(client).stream(s, "sys", "hi", 50, got.append))
    assert "".join(got) == "Hi there" and c.tokens_in == 12 and c.tokens_out == 4 and c.cached_tokens == 3


def test_stream_error_status_raises():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    from router.registry import ModelSpec
    s = ModelSpec(id="x/y", provider="openai_compatible", model="y", tier=1, context_window=1000,
                  capabilities=set(), price_in=1, price_out=1, latency_ms_p50=100, prior={}, base_url="https://example.test/v1")
    with pytest.raises(ProviderError, match="429"):
        asyncio.run(OpenAIProvider(client, compatible=True).stream(s, "sys", "hi", 50, lambda t: None))


class _FreeRefuses(Provider):
    def __init__(self):
        self.calls = []

    async def complete(self, spec, system, user, max_tokens, **kw):
        self.calls.append(spec.id)
        if spec.extra.get("free"):
            raise ProviderError("openai_compatible HTTP 429: free-models-per-day limit reached")
        return Completion("Why did the chicken cross the road? To get to the other side.", 20, 15, 5.0, "stop")


def test_free_refusal_falls_back_to_paid_and_streams(tmp_path):
    reg, _ = _registry(tmp_path, ["anthropic", "openai"])
    (tmp_path / "root" / "config").mkdir(parents=True)
    (tmp_path / "root" / "config" / "models.yaml").write_text((tmp_path / "m.yaml").read_text())
    prov = _FreeRefuses()
    r = Router(tmp_path / "root", db_path=tmp_path / "r.db",
               providers={"openai_compatible": prov, "anthropic": prov, "openai": prov, "offline": prov})
    deltas = []
    res = asyncio.run(r.run(RunRequest("tell me a joke", use_cache=False), on_delta=lambda n, t: deltas.append(n)))
    assert prov.calls[0].endswith(":free") and res.passed and not reg.models[res.model].extra.get("free")
    assert deltas and deltas[-1] > 1                                # second attempt streamed
    assert catalog.FREE.snapshot()["pause_reason"] == "daily"
    # next easy question doesn't even try the free model
    prov.calls.clear()
    asyncio.run(r.run(RunRequest("tell me another joke", use_cache=False)))
    assert not prov.calls[0].endswith(":free")

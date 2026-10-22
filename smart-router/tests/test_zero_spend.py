# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""The no-spend promise: with paid models off (the default), nothing that bills per use is
ever called: not as the first choice, not on escalation, not for reviews, not by name."""
from __future__ import annotations

import asyncio

import pytest

from router import catalog
from router.engine import Router, RunRequest
from router.providers import Completion, OpenAIProvider, Provider
from router.registry import ModelSpec
from router.selector import NoFreeModel, route
from router.classifier import classify

PRIOR = {k: 0.85 for k in ["coding", "research", "writing", "document", "data", "chat"]}


class Scripted(Provider):
    def __init__(self, replies):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.calls: list[tuple[str, int]] = []

    async def complete(self, spec, system, user, max_tokens, **kw):
        self.calls.append((spec.id, max_tokens))
        q = self.replies.get(spec.id) or ["ok"]
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, tuple):                         # (text, tokens_out)
            return Completion(r[0], 500, r[1], 5.0, "length")
        return Completion(r, 500, 200, 5.0, "end_turn")


def _free(mid: str) -> ModelSpec:
    return ModelSpec(id=mid, provider="anthropic", model=mid, tier=1, context_window=200000,
                     capabilities={"json", "tools"}, price_in=0, price_out=0, latency_ms_p50=900,
                     prior=dict(PRIOR), api_key_env="ANTHROPIC_API_KEY",
                     extra={"free": True, "trains": False, "name": mid, "via": "free provider"})


@pytest.fixture
def paid_off(monkeypatch):
    monkeypatch.delenv("PROMPTARO_ALLOW_PAID", raising=False)   # the real default


@pytest.fixture
def make(tmp_path, monkeypatch, test_root):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    def _make(replies, free_ids=("free-a",)):
        fake = Scripted(replies)
        r = Router(test_root, db_path=tmp_path / "t.db", providers={"anthropic": fake, "openai": fake,
                                                                "openai_compatible": fake, "offline": fake})
        for fid in free_ids:
            r.registry.models[fid] = _free(fid)
        return r, fake
    return _make


def paid_ids(r) -> set[str]:
    return {m.id for m in r.registry.models.values() if catalog.costs_money(m)}


def test_paid_models_are_off_by_default(paid_off):
    assert catalog.get_settings()["allow_paid"] is False
    assert catalog.paid_allowed() is False


def test_routing_never_offers_a_paid_model(paid_off, make):
    r, _ = make({})
    for text in ["say hello", "design a multi-region failover plan for a payments platform with zero data loss"]:
        d = route(r.registry, r.store, classify(text))
        ids = {c.model.id for c in [d.primary, *d.ladder, *d.considered]}
        assert ids and not (ids & paid_ids(r)), ids


def test_failed_free_answer_never_escalates_to_paid(paid_off, make):
    """The bug that charged $0.10: a free answer failed its checks and the ladder jumped to the
    priciest model. Now the ladder holds only free models."""
    r, fake = make({"free-a": [""], "free-b": [""]}, free_ids=("free-a", "free-b"))
    res = asyncio.run(r.run(RunRequest(instruction="say hello", use_cache=False)))
    called = {mid for mid, _ in fake.calls}
    assert called and not (called & paid_ids(r)), called
    assert res.total_cost == 0


def test_nothing_free_left_gives_a_clear_message_not_a_charge(paid_off, make):
    r, fake = make({}, free_ids=())
    with pytest.raises(NoFreeModel) as e:
        asyncio.run(r.run(RunRequest(instruction="say hello", use_cache=False)))
    assert "won't spend money" in str(e.value)
    assert fake.calls == []


def test_picking_a_paid_model_by_name_is_refused(paid_off, make):
    r, fake = make({})
    with pytest.raises(NoFreeModel) as e:
        asyncio.run(r.run(RunRequest(instruction="say hello", force_model="claude-opus", use_cache=False)))
    assert "paid model" in str(e.value) and fake.calls == []


def test_opting_in_allows_paid_models(paid_off, make):
    catalog.set_settings(allow_paid=True)
    r, _ = make({}, free_ids=())
    d = route(r.registry, r.store, classify("say hello"))
    assert d.primary.model.id in paid_ids(r)


def test_reviews_never_use_a_paid_model(paid_off, make):
    r, _ = make({})
    pool = r._secondary_pool(RunRequest(instruction="x"))
    assert pool and not ({m.id for m in pool} & paid_ids(r))


def test_empty_thinking_reply_gets_more_room_on_the_same_free_model(paid_off, make):
    """A reasoning model that spends its allowance thinking returns no text. Retry the same model
    with a bigger allowance instead of treating it as a failure."""
    r, fake = make({"free-a": [("", 900), "Hello there!"]})
    res = asyncio.run(r.run(RunRequest(instruction="say hello", use_cache=False)))
    assert res.passed and res.output == "Hello there!"
    assert [mid for mid, _ in fake.calls] == ["free-a", "free-a"]
    assert fake.calls[1][1] > fake.calls[0][1]


def test_openrouter_requests_ask_for_brief_hidden_reasoning():
    seen = {}

    class Client:
        async def post(self, url, json=None, headers=None, timeout=None):
            seen.update(json)

            class R:
                status_code = 200
                headers = {}

                def json(self):
                    return {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
            return R()

    spec = ModelSpec(id="x", provider="openai_compatible", model="m", tier=1, context_window=8000,
                     capabilities=set(), price_in=0, price_out=0, latency_ms_p50=1, prior={},
                     api_key_env="", base_url="https://openrouter.ai/api/v1", extra={})
    asyncio.run(OpenAIProvider(Client(), compatible=True).complete(spec, "s", "u", 100))
    assert seen["reasoning"] == {"effort": "low", "exclude": True}

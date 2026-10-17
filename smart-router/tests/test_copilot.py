# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""GitHub Copilot plan lane: device sign-in, service-token exchange, catalog inclusion."""
from __future__ import annotations

import asyncio
import time

import httpx
import yaml

from router import copilot, secrets
from router.catalog import free_provider_specs
from router.providers import CopilotProvider
from router.registry import ModelSpec

SPEC = ModelSpec(id="github_copilot/gpt-4o", provider="github_copilot", model="gpt-4o", tier=2,
                 context_window=200000, capabilities={"json", "tools"}, price_in=0.0, price_out=0.0,
                 latency_ms_p50=1800, prior={}, api_key_env="GITHUB_COPILOT_TOKEN",
                 base_url="https://api.githubcopilot.com",
                 extra={"quota": "github_copilot", "plan": True})


def _github(pending=False):
    """MockTransport standing in for github.com, api.github.com and api.githubcopilot.com."""
    calls = {"token": 0, "svc": 0, "chat": 0}

    def handler(r: httpx.Request) -> httpx.Response:
        host, path = r.url.host, r.url.path
        if host == "github.com" and path == "/login/device/code":
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-1234",
                                             "verification_uri": "https://github.com/login/device",
                                             "interval": 1, "expires_in": 900})
        if host == "github.com" and path == "/login/oauth/access_token":
            calls["token"] += 1
            if pending and calls["token"] == 1:
                return httpx.Response(200, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "gho_test"})
        if host == "api.github.com" and path == "/copilot_internal/v2/token":
            calls["svc"] += 1
            assert r.headers["authorization"] == "token gho_test"
            return httpx.Response(200, json={"token": "tid=svc;exp=1", "expires_at": time.time() + 1500,
                                             "endpoints": {"api": "https://api.individual.githubcopilot.com"}})
        if host.endswith("githubcopilot.com") and path == "/models":
            assert r.headers["authorization"] == "Bearer tid=svc;exp=1"
            return httpx.Response(200, json={"data": [
                {"id": "gpt-4o", "capabilities": {"type": "chat"}},
                {"id": "claude-sonnet-4", "capabilities": {"type": "chat"}},
                {"id": "text-embedding-3", "capabilities": {"type": "embeddings"}}]})
        if host.endswith("githubcopilot.com") and path == "/chat/completions":
            calls["chat"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"},
                                                          "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        return httpx.Response(404, json={"unexpected": f"{host}{path}"})
    return calls, handler


def test_device_flow_pending_then_connected():
    calls, handler = _github(pending=True)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    d = copilot.start_device(client)
    assert d["user_code"] == "ABCD-1234"
    assert copilot.poll_device(client)["status"] == "pending"
    assert copilot.poll_device(client)["status"] == "connected"
    assert secrets.get("GITHUB_COPILOT_TOKEN") == "gho_test"


def test_device_flow_denied_clears_state():
    def handler(r):
        if r.url.path == "/login/device/code":
            return httpx.Response(200, json={"device_code": "dc", "user_code": "X", "interval": 1})
        return httpx.Response(200, json={"error": "access_denied", "error_description": "User said no"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    copilot.start_device(client)
    d = copilot.poll_device(client)
    assert d["status"] == "error" and "no" in d["detail"].lower()
    assert copilot.poll_device(client)["status"] == "error"   # state cleared, not re-pollable


def test_service_token_cached_and_models_listed():
    calls, handler = _github()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    secrets.set("GITHUB_COPILOT_TOKEN", "gho_test")
    ids = copilot.list_models(client)
    assert ids == ["gpt-4o", "claude-sonnet-4"]                # embeddings filtered out
    copilot.service_token(client)
    copilot.service_token(client)
    assert calls["svc"] == 1                                   # second exchange served from cache


def test_catalog_includes_plan_models_only_when_signed_in():
    assert not [s for s in free_provider_specs() if s["id"].startswith("github_copilot/")]
    secrets.set("GITHUB_COPILOT_TOKEN", "gho_test")
    calls, handler = _github()
    from router import catalog
    ids = catalog.discover_free("github_copilot", httpx.Client(transport=httpx.MockTransport(handler)))
    specs = {s["id"]: s for s in free_provider_specs()}
    assert "github_copilot/gpt-4o" in specs and "github_copilot/claude-sonnet-4" in specs
    s = specs["github_copilot/gpt-4o"]
    assert s["provider"] == "github_copilot" and s["plan"] and s["free"]
    assert s["via"].endswith("(plan)") and s["price"]["input"] == 0.0
    assert "gpt-4o" in ids and "claude-sonnet-4" in ids


def test_copilot_provider_exchanges_and_completes():
    async def go():
        calls, handler = _github()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            prov = CopilotProvider(client, compatible=True)
            c = await prov.complete(SPEC, "sys", "hello", 100)
            assert c.text == "hi" and c.tokens_in == 10
            assert calls["chat"] == 1 and calls["svc"] == 1
            await prov.complete(SPEC, "sys", "again", 100)
            assert calls["svc"] == 1                           # cached service token
    secrets.set("GITHUB_COPILOT_TOKEN", "gho_test")
    asyncio.run(go())


def test_plan_models_survive_free_models_off(tmp_path, monkeypatch):
    """free_models=off disables free tiers — a signed-in plan is not a free tier."""
    from router import catalog, selector, store
    monkeypatch.setattr(catalog, "get_settings", lambda: {"free_models": "off", "gateway_spend": "free",
                                                        "daily_budget_usd": 0.0})
    cfg = {"catalog": {"enabled": False},
           "models": [dict(id="github_copilot/gpt-4o", provider="github_copilot", model="gpt-4o", tier=2,
                           api_key_env="GITHUB_COPILOT_TOKEN", free=True, plan=True, quota="github_copilot",
                           capabilities=["json", "tools"], price={"input": 0, "output": 0}),
                      dict(id="groq/llama", provider="openai_compatible", model="llama", tier=1,
                           api_key_env="GROQ_API_KEY", free=True, quota="groq",
                           capabilities=["json"], price={"input": 0, "output": 0})]}
    cfgp = tmp_path / "models.yaml"
    cfgp.write_text(yaml.safe_dump(cfg))
    secrets.set("GITHUB_COPILOT_TOKEN", "gho_test")
    secrets.set("GROQ_API_KEY", "gsk_x")
    from router.registry import Registry
    reg = Registry(cfgp, use_catalog=False)
    from router.classifier import TaskProfile
    d = selector.route(reg, store.Store(tmp_path / "s.json"),
                       TaskProfile(task_type="chat", complexity=0.3, confidence=0.9), allow_free=False)
    assert d.primary.model.id == "github_copilot/gpt-4o"       # plan model survived; groq/llama filtered out

# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Free providers, the free-tier ledger, and the coding-tools gateway. Author: Akshay Dubey"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from router import catalog, gateway, quota, secrets
from router.classifier import classify
from router.engine import Router
from router.free_providers import FREE_PROVIDERS, detect, pick_models
from router.registry import Registry
from router.selector import route

REPO = Path(__file__).resolve().parents[1]


def _root(tmp_path, extra_models=()):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO / "skills", root / "skills", dirs_exist_ok=True)
    cfg = yaml.safe_load((REPO / "config" / "models.yaml").read_text())
    cfg["catalog"]["enabled"] = True
    cfg["catalog"]["ollama"] = "off"
    cfg["models"] = list(extra_models) + cfg["models"]
    (root / "config" / "models.yaml").write_text(yaml.safe_dump(cfg))
    catalog._write_cache("openrouter_models.json", [])                # no network
    return root


def _free_stack():
    secrets.set("GROQ_API_KEY", "gsk_" + "a" * 40)
    secrets.set("CEREBRAS_API_KEY", "csk-" + "b" * 40)
    secrets.set("MISTRAL_API_KEY", "m" * 32)
    catalog._write_cache("free_groq.json", ["openai/gpt-oss-120b", "llama-3.1-8b-instant", "whisper-large-v3",
                                            "meta-llama/llama-prompt-guard-2-22m"])
    catalog._write_cache("free_cerebras.json", ["gpt-oss-120b"])
    catalog._write_cache("free_mistral.json", ["devstral-medium-latest", "mistral-small-latest"])


PAID = {"id": "paid/big", "name": "Paid Big", "provider": "openai_compatible", "model": "big",
        "api_key_env": "PAID_KEY", "base_url": "https://paid.test/v1", "tier": 3, "context_window": 200000,
        "capabilities": ["json", "tools", "long_context"], "price": {"input": 3, "output": 15},
        "latency_ms_p50": 2000, "prior": {k: 0.95 for k in ["coding", "research", "writing", "document", "data", "chat"]}}


# ------------------------------------------------------------- registry
@pytest.mark.parametrize("key,pid", [("gsk_" + "x" * 40, "groq"), ("csk-" + "y" * 40, "cerebras"),
                                     ("nvapi-" + "z" * 40, "nvidia"), ("sk-or-v1-abc", None)])
def test_free_key_detection(key, pid):
    assert detect(key) == pid


def test_pick_models_skips_non_chat_and_keeps_best():
    picks = pick_models(FREE_PROVIDERS["groq"], ["openai/gpt-oss-120b", "whisper-large-v3",
                                                 "meta-llama/llama-prompt-guard-2-86m", "llama-3.1-8b-instant"])
    assert [m for m, _ in picks] == ["openai/gpt-oss-120b", "llama-3.1-8b-instant"]


def test_free_provider_specs(tmp_path):
    _free_stack()
    reg = Registry(_root(tmp_path) / "config" / "models.yaml")
    g = reg.models["groq/openai/gpt-oss-120b"]
    assert g.extra["free"] and g.extra["quota"] == "groq" and not g.extra["trains"] and "tools" in g.capabilities
    assert g.extra["limits"]["tpm"] == 8000 and g.base_url.startswith("https://api.groq.com")
    assert reg.models["mistral/devstral-medium-latest"].extra["trains"] is True
    assert "groq/whisper-large-v3" not in reg.models


# --------------------------------------------------------------- ledger
def _spec(tmp_path, mid="groq/openai/gpt-oss-120b"):
    _free_stack()
    return Registry(_root(tmp_path) / "config" / "models.yaml").models[mid]


def test_ledger_blocks_requests_that_cannot_fit(tmp_path):
    g = _spec(tmp_path)
    ok, why = quota.LEDGER.allow(g, 25_000)                  # a Claude Code turn on an 8k-TPM model
    assert not ok and "too large" in why
    assert quota.LEDGER.allow(g, 2000)[0]


def test_ledger_counts_minute_and_day(tmp_path):
    c = _spec(tmp_path, "cerebras/gpt-oss-120b")              # 5 requests a minute
    for _ in range(5):
        assert quota.LEDGER.allow(c, 100)[0]
        quota.LEDGER.record(c, 100)
    ok, why = quota.LEDGER.allow(c, 100)
    assert not ok and "per-minute" in why


def test_ledger_reads_provider_headers(tmp_path):
    g = _spec(tmp_path)
    quota.LEDGER.record(g, 500, {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2h1m",
                                 "x-ratelimit-remaining-tokens": "7500", "x-ratelimit-reset-tokens": "7.66s"})
    ok, why = quota.LEDGER.allow(g, 100)
    assert not ok and "provider says" in why
    snap = quota.LEDGER.snapshot([g])[0]
    assert snap["models"][0]["requests_left_today"] == 0


def test_ledger_pauses_on_refusal(tmp_path):
    g = _spec(tmp_path)
    quota.LEDGER.refused(g, 12, "429 rate limit reached on tokens per minute (TPM)")
    assert not quota.LEDGER.allow(g, 10)[0]
    assert 0 < quota.LEDGER.paused[quota.Ledger.scope(g)][0] - time.time() <= 12.5
    c = _spec(tmp_path, "cerebras/gpt-oss-120b")
    quota.LEDGER.refused(c, None, "Tokens per day limit exceeded (TPD)")
    assert quota.LEDGER.paused[quota.Ledger.scope(c)][1] == "daily"


def test_ledger_survives_restart(tmp_path):
    led = quota.Ledger(tmp_path / "l.json")
    g = _spec(tmp_path)
    led.record(g, 1234)
    assert quota.Ledger(tmp_path / "l.json").daily[quota.Ledger.scope(g)] == [1, 1234]


@pytest.mark.parametrize("v,s", [("2m59.56s", 179.56), ("7.66s", 7.66), ("1h2m", 3720), ("450ms", 0.45), ("12", 12)])
def test_parse_reset(v, s):
    assert quota.parse_reset(v) == pytest.approx(s)


# ------------------------------------------------------------- routing
def test_code_skips_free_providers_that_train(tmp_path):
    _free_stack()
    reg = Registry(_root(tmp_path) / "config" / "models.yaml")
    st = __import__("router.store", fromlist=["Store"]).Store(tmp_path / "s.db")
    prof = classify("Refactor the payment module and add tests for the retry edge cases")
    picked = {c.model.id for c in route(reg, st, prof, private=True).considered}
    assert not any(m.startswith("mistral/") for m in picked)
    assert any(m.startswith("mistral/") for m in {c.model.id for c in route(reg, st, prof).considered})


def test_require_tools(tmp_path):
    _free_stack()
    reg = Registry(_root(tmp_path, [dict(PAID, id="paid/notools", capabilities=["json"])]) / "config" / "models.yaml")
    secrets.set("PAID_KEY", "k")
    st = __import__("router.store", fromlist=["Store"]).Store(tmp_path / "s.db")
    d = route(reg, st, classify("hi"), require={"tools"})
    assert "paid/notools" not in {c.model.id for c in d.considered}


def test_sticky_session_keeps_the_conversation_on_one_model(tmp_path):
    _free_stack()
    root = _root(tmp_path, [PAID])
    secrets.set("PAID_KEY", "k")
    r = Router(root, db_path=tmp_path / "r.db")
    msgs = [{"role": "user", "content": "this conversation's first message"}]
    kw = dict(messages=msgs, est_in=100, est_out=50, tools=False, images=False, requested="tillwright-auto")
    cands = gateway.plan(r, **kw)
    assert len(cands) > 1
    last = cands[-1].id
    gateway._sticky_pick(gateway._stick_key(msgs, False), last)      # pretend this model served turn 1
    cands2 = gateway.plan(r, **kw)
    assert cands2[0].id == last                                      # turn 2 prefers it
    other = [{"role": "user", "content": "a different conversation"}]
    cands3 = gateway.plan(r, messages=other, est_in=100, est_out=50, tools=False, images=False,
                          requested="tillwright-auto")
    assert cands3[0].id == cands[0].id                               # other conversations unaffected


# --------------------------------------------------------- translation
def test_anthropic_request_translation():
    body = {"system": [{"type": "text", "text": "You are Claude Code."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Read a.py"},
                                             {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                          "data": "AAA"}}]},
                {"role": "assistant", "content": [{"type": "text", "text": "Reading."},
                                                  {"type": "tool_use", "id": "toolu_1", "name": "Read",
                                                   "input": {"file_path": "a.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "print(1)"},
                                             {"type": "text", "text": "now fix it"}]}],
            "tools": [{"name": "Read", "description": "read", "input_schema": {"$schema": "x", "type": "object",
                                                                             "properties": {"file_path": {"type": "string"}}}},
                      {"type": "web_search_20250305", "name": "web_search"}],
            "tool_choice": {"type": "any"}, "stop_sequences": ["END"], "temperature": 0.2}
    o = gateway.anthropic_to_openai(body)
    roles = [m["role"] for m in o["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert o["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"
    call = o["messages"][2]["tool_calls"][0]
    assert call["id"] == "toolu_1" and json.loads(call["function"]["arguments"]) == {"file_path": "a.py"}
    assert o["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "print(1)"}
    assert [t["function"]["name"] for t in o["tools"]] == ["Read"] and "$schema" not in o["tools"][0]["function"]["parameters"]
    assert o["tool_choice"] == "required" and o["stop"] == ["END"]
    short = gateway.anthropic_to_openai(body, short_ids=True)
    sid = short["messages"][2]["tool_calls"][0]["id"]
    assert len(sid) == 9 and sid.isalnum() and short["messages"][3]["tool_call_id"] == sid


def _events(lines: list[str]) -> list[dict]:
    out = []
    for blob in lines:
        for part in blob.strip().split("\n\n"):
            ev = [l for l in part.split("\n") if l.startswith("data: ")]
            if ev:
                out.append(json.loads(ev[0][6:]))
    return out


def test_stream_translation_text_and_tools():
    tr = gateway.StreamTranslator("claude-sonnet-4-5", 100)
    out = [tr.start()]
    for ch in [{"choices": [{"delta": {"content": "Let me "}}]}, {"choices": [{"delta": {"content": "look."}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "Read", "arguments": "{\"file_"}}]}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "path\": \"a.py\"}"}}]}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "call_10", "function": {"name": "Bash", "arguments": "{bad json"}}]}}]},
               {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
               {"choices": [], "usage": {"prompt_tokens": 321, "completion_tokens": 45}}]:
        out += tr.feed(ch)
    out += tr.end()
    ev = _events(out)
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "message_start" and kinds[-1] == "message_stop"
    text = "".join(e["delta"]["text"] for e in ev if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta")
    assert text == "Let me look."
    tools = [e["content_block"] for e in ev if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
    assert [t["name"] for t in tools] == ["Read", "Bash"] and tools[0]["id"] == "call_9"
    args = [json.loads(e["delta"]["partial_json"]) for e in ev if e.get("delta", {}).get("type") == "input_json_delta"]
    assert args == [{"file_path": "a.py"}, {}]                    # malformed arguments repaired to an object
    md = next(e for e in ev if e["type"] == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use" and md["usage"] == {"input_tokens": 321, "output_tokens": 45}
    starts = [e["index"] for e in ev if e["type"] == "content_block_start"]
    assert starts == [0, 1, 2]


def test_nonstream_translation():
    m = gateway.openai_to_anthropic({"choices": [{"message": {"content": "ok", "tool_calls": [
        {"id": "c1", "function": {"name": "Write", "arguments": "{\"a\":1}"}}]}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}}, "claude-haiku")
    assert m["stop_reason"] == "tool_use" and m["content"][1]["input"] == {"a": 1} and m["usage"]["output_tokens"] == 2


# ---------------------------------------------------------- end to end
def _sse_body(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


@pytest.fixture
def gw(tmp_path, monkeypatch):
    _free_stack()
    root = _root(tmp_path, [PAID])
    secrets.set("PAID_KEY", "paid-key")
    calls = []

    def upstream(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((req.url.host, body.get("model"), body))
        if len(calls) == 1:                                   # the first choice is always rate-limited
            return httpx.Response(429, headers={"retry-after": "20"},
                                  text='{"error":{"message":"Rate limit reached for tokens per minute"}}')
        if req.url.host == "api.groq.com" and False:
            return httpx.Response(429, headers={"retry-after": "20"}, text='{"error":{"message":"Rate limit reached for tokens per minute"}}')
        if body.get("stream"):
            return httpx.Response(200, headers={"x-ratelimit-remaining-requests": "999"}, text=_sse_body(
                {"choices": [{"delta": {"content": "Hello from "}}]}, {"choices": [{"delta": {"content": req.url.host}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 6}}))
        return httpx.Response(200, json={"choices": [{"message": {"content": f"Hi from {req.url.host}"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 40, "completion_tokens": 6}})
    monkeypatch.setattr(gateway, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    app = FastAPI()
    app.state.router = Router(root, db_path=tmp_path / "r.db")
    app.include_router(gateway.api)
    usage = []
    app.state.router.usage_listeners.append(lambda *a: usage.append(a))
    c = TestClient(app)
    c.calls, c.usage, c.token = calls, usage, gateway.gateway_token()
    return c


def test_gateway_requires_token(gw):
    assert gw.post("/v1/messages", json={"model": "x", "messages": []}).status_code == 401
    assert gw.post("/v1/messages", json={"model": "x", "messages": []}, headers={"x-api-key": "wrong"}).status_code == 401
    assert gw.get("/v1/models", headers={"authorization": f"Bearer {gw.token}"}).status_code == 200


def test_claude_code_style_request_fails_over_instantly_and_streams(gw):
    t0 = time.time()
    r = gw.post("/v1/messages?beta=true", headers={"x-api-key": gw.token, "anthropic-version": "2023-06-01"},
                json={"model": "claude-haiku-4-5", "max_tokens": 512, "stream": True,
                      "messages": [{"role": "user", "content": "write a 5 word title for: fixing a typo"}]})
    assert r.status_code == 200 and time.time() - t0 < 5          # no back-off waiting on the refusal
    ev = _events([r.text])
    text = "".join(e["delta"]["text"] for e in ev if e.get("type") == "content_block_delta")
    assert text.startswith("Hello from")
    hosts = [h for h, m, _ in gw.calls]
    assert len(hosts) == 2 and (hosts[0], gw.calls[0][1]) != (hosts[1], gw.calls[1][1])   # moved on at once
    assert "paid.test" not in hosts                                    # free only by default
    first = {"api.groq.com": "groq", "api.cerebras.ai": "cerebras"}[hosts[0]] + "/" + gw.calls[0][1]
    assert r.headers["x-tillwright-model"] != first
    assert len(quota.LEDGER.paused) == 1                               # only the refusing model is paused
    # the next request doesn't touch the paused model
    gw.calls.clear()
    gw.post("/v1/messages", headers={"x-api-key": gw.token},
            json={"model": "claude-haiku-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]})
    assert gw.calls and all(f"{h}" for h, _, _ in gw.calls)
    assert gw.usage and gw.usage[-1][1] == 40 and gw.usage[-1][2] == 6


def test_big_agent_request_skips_small_tpm_models_without_calling_them(gw):
    gw.calls.append(("warm", "", {}))                                   # don't trigger the first-call refusal
    big = "x" * 80_000                                                  # ~22k tokens, like a Claude Code turn
    r = gw.post("/v1/messages", headers={"x-api-key": gw.token},
                json={"model": "claude-sonnet-4-5", "max_tokens": 4096, "system": big,
                      "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
                      "messages": [{"role": "user", "content": "fix the failing test"}]})
    assert r.status_code == 200, r.text
    assert "api.groq.com" not in [h for h, _, _ in gw.calls]           # 8k tokens/min can't fit 22k
    assert r.json()["content"][0]["text"] == "Hi from api.cerebras.ai"


def test_request_too_big_for_every_free_model_says_so(gw):
    gw.calls.append(("warm", "", {}))
    r = gw.post("/v1/messages", headers={"x-api-key": gw.token},
                json={"model": "claude-sonnet-4-5", "max_tokens": 4096, "system": "x" * 140_000,
                      "messages": [{"role": "user", "content": "fix it"}]})
    assert r.status_code == 400 and len(gw.calls) == 1                 # no doomed calls were made


def test_paid_models_only_when_allowed(gw):
    gw.calls.append(("warm", "", {}))
    catalog.set_settings(gateway_spend="paid_ok")
    r = gw.post("/v1/messages", headers={"x-api-key": gw.token},
                json={"model": "claude-opus-4-5", "max_tokens": 256,
                      "messages": [{"role": "user", "content": "Design a distributed rate limiter with "
                                                               "security trade-offs and a migration plan"}]})
    assert r.status_code == 200 and "paid.test" in [h for h, _, _ in gw.calls]


def test_openai_chat_completions_passthrough(gw):
    gw.calls.append(("warm", "", {}))
    r = gw.post("/v1/chat/completions", headers={"authorization": f"Bearer {gw.token}"},
                json={"model": "tillwright-auto", "stream": True,
                      "messages": [{"role": "user", "content": "say hi"}]})
    assert r.status_code == 200 and "Hello from" in r.text and "[DONE]" in r.text
    sent = gw.calls[-1][2]
    assert sent["stream_options"] == {"include_usage": True} and sent["model"] != "tillwright-auto"


def test_count_tokens_is_local(gw):
    r = gw.post("/v1/messages/count_tokens", headers={"x-api-key": gw.token},
                json={"model": "m", "messages": [{"role": "user", "content": "hello " * 100}]})
    assert r.status_code == 200 and r.json()["input_tokens"] > 50 and gw.calls == []     # no model called


def test_nothing_available_explains(gw):
    for pid in ("groq", "cerebras", "mistral"):
        secrets.delete(FREE_PROVIDERS[pid].key_env)
    gw.app.state.router.reload()
    r = gw.post("/v1/messages", headers={"x-api-key": gw.token},
                json={"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "Connections" in r.json()["error"]["message"]


def test_bad_keys_are_reported_not_retried(gw, monkeypatch):
    def refuse(req):
        return httpx.Response(401, text='{"error":{"message":"Invalid API key"}}')
    monkeypatch.setattr(gateway, "_client", httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    r = gw.post("/v1/messages", headers={"x-api-key": gw.token},
                json={"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400 and "check the keys" in r.json()["error"]["message"]


def test_busy_models_ask_client_to_retry(gw, monkeypatch):
    def busy(req):
        return httpx.Response(503, text="overloaded")
    monkeypatch.setattr(gateway, "_client", httpx.AsyncClient(transport=httpx.MockTransport(busy)))
    r = gw.post("/v1/messages", headers={"x-api-key": gw.token},
                json={"model": "claude-sonnet-4-5", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 529

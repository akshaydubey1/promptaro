# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Sign-in flow, key storage and live catalog — all with mocked network."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import hashlib
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from router import catalog, connect, secrets

NOW = int(time.time())


def m(mid, p_in, p_out, created, ctx=200000, img=True):
    return {"id": mid, "name": mid, "created": created, "context_length": ctx,
            "pricing": {"prompt": str(p_in / 1e6), "completion": str(p_out / 1e6)},
            "architecture": {"input_modalities": ["text", "image"] if img else ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools", "response_format"]}


FAKE_CATALOG = [
    m("anthropic/claude-small-9", 1, 5, NOW - 100),
    m("anthropic/claude-mid-9", 3, 15, NOW - 200),
    m("anthropic/claude-big-9", 5, 25, NOW - 300),
    m("anthropic/claude-big-9:batch", 2, 10, NOW - 300),       # variant: skipped
    m("anthropic/claude-ancient-1", 0.1, 0.1, NOW - 900 * 86400),  # too old: skipped
    m("openai/gpt-tiny", 0.2, 0.8, NOW - 50),
    m("openai/gpt-huge", 10, 40, NOW - 60),
    m("openai/gpt-audio-x", 1, 1, NOW),                        # audio: skipped
    m("openrouter/auto", -1, -1, NOW),                         # router pseudo-model: skipped
]


@pytest.fixture
def fake_catalog(isolated_secrets):
    catalog._write_cache("openrouter_models.json", FAKE_CATALOG)
    return FAKE_CATALOG


# ------------------------------------------------------------------ secrets
def test_secret_roundtrip_and_permissions(isolated_secrets):
    secrets.set("OPENROUTER_API_KEY", "sk-or-abc")
    assert secrets.get("OPENROUTER_API_KEY") == "sk-or-abc"
    f = isolated_secrets / "credentials.json"
    if os.name == "posix":          # Windows has no rwx bits; protection is by user profile
        assert oct(f.stat().st_mode)[-3:] == "600"
    secrets.delete("OPENROUTER_API_KEY")
    assert not secrets.has("OPENROUTER_API_KEY")


def test_env_overrides_store(monkeypatch):
    secrets.set("OPENAI_API_KEY", "stored")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    assert secrets.get("OPENAI_API_KEY") == "from-env"


# ------------------------------------------------------------------ catalog
def test_choose_picks_small_mid_large_and_skips_junk(fake_catalog):
    picks = catalog.choose(fake_catalog, ["anthropic", "openai"])
    ids = {(v, t, x["id"]) for v, t, x in picks}
    assert ("anthropic", 1, "anthropic/claude-small-9") in ids
    assert ("anthropic", 2, "anthropic/claude-mid-9") in ids
    assert ("anthropic", 3, "anthropic/claude-big-9") in ids
    assert ("openai", 3, "openai/gpt-huge") in ids
    assert all(":" not in x["id"] and "audio" not in x["id"] and "ancient" not in x["id"] for _, _, x in picks)


def test_no_connection_means_no_models(fake_catalog):
    assert catalog.build_specs({"vendors": ["anthropic", "openai"]}) == []


def test_cached_only_build_specs_never_hits_network(fake_catalog, monkeypatch):
    """Startup path: serve the disk cache instantly; the real refresh runs in the background."""
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    monkeypatch.setattr(catalog, "fetch_openrouter_models",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network on cached path")))
    specs = {s["id"]: s for s in catalog.build_specs({"vendors": ["anthropic"]}, cached_only=True)}
    assert "anthropic/claude-small-9" in specs


def test_openrouter_signin_unlocks_all_vendors_with_real_prices(fake_catalog):
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    specs = {s["id"]: s for s in catalog.build_specs({"vendors": ["anthropic", "openai"]})}
    assert {"anthropic/claude-small-9", "openai/gpt-huge"} <= set(specs)
    s = specs["anthropic/claude-mid-9"]
    assert s["via"] == "openrouter" and s["base_url"].startswith("https://openrouter.ai")
    assert s["price"] == {"input": pytest.approx(3.0), "output": pytest.approx(15.0)}
    assert "vision" in s["capabilities"] and "long_context" in s["capabilities"]


def test_direct_key_preferred_over_openrouter(fake_catalog):
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    secrets.set("ANTHROPIC_API_KEY", "sk-ant-x")
    catalog._write_cache("native_anthropic.json", ["claude-small-9-20260101", "claude-mid-9", "claude-big-9"])
    specs = {s["id"]: s for s in catalog.build_specs({"vendors": ["anthropic", "openai"]})}
    assert specs["anthropic/claude-small-9"]["via"] == "direct"
    assert specs["anthropic/claude-small-9"]["model"] == "claude-small-9-20260101"   # dated snapshot matched
    assert specs["anthropic/claude-mid-9"]["provider"] == "anthropic"
    assert specs["openai/gpt-tiny"]["via"] == "openrouter"                            # no OpenAI key


# ---------------------------------------------------------------------- API
class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=self)


class FakeAsyncClient:
    posted: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aclose(self):
        pass

    async def post(self, url, json=None, **k):
        FakeAsyncClient.posted.append((url, json))
        return FakeResp({"key": "sk-or-from-oauth"})

    async def get(self, url, headers=None, **k):
        ok = headers and headers.get("authorization") in ("Bearer good-key", "Bearer sk-or-good-key-123")
        return FakeResp({"data": {}} if ok else {"error": {"message": "No auth credentials found"}}, 200 if ok else 401)


@pytest.fixture
def client(fake_catalog, monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTER_DB", str(tmp_path / "api.db"))
    monkeypatch.setattr(connect.httpx, "AsyncClient", FakeAsyncClient)
    from router import api
    with TestClient(api.app) as c:
        c.headers.update({"x-promptaro": "1"})       # the app's own calls carry the CSRF header
        yield c


def test_ui_served_and_shows_signin_when_nothing_connected(client):
    assert "Sign in with OpenRouter" in client.get("/").text
    s = client.get("/connect/status").json()
    assert not any(v["connected"] for v in s["connections"].values())
    assert s["models"] == []


def test_oauth_pkce_full_flow(client):
    r = client.get("/connect/openrouter/start", follow_redirects=False)
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert loc.netloc == "openrouter.ai" and q["code_challenge_method"] == "S256"
    cb = urlparse(q["callback_url"])
    assert cb.hostname in ("testserver", "localhost")
    nonce = cb.path.rsplit("/", 1)[1]
    verifier = connect._pending[nonce][0]
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected

    r = client.get(f"{cb.path}?code=abc123", follow_redirects=False)
    assert r.headers["location"] == "/?connected=openrouter"
    url, body = FakeAsyncClient.posted[-1]
    assert url == connect.OR_EXCHANGE and body["code"] == "abc123" and body["code_verifier"] == verifier
    assert secrets.get("OPENROUTER_API_KEY") == "sk-or-from-oauth"

    s = client.get("/connect/status").json()
    assert s["connections"]["openrouter"]["connected"]
    assert any(m["usable"] and m["via"] == "openrouter" for m in s["models"])
    # The key itself is never exposed
    assert "sk-or-from-oauth" not in client.get("/connect/status").text

    # Replayed callback is refused
    r = client.get(f"{cb.path}?code=abc123", follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_bad_pasted_key_is_rejected_and_not_kept(client):
    r = client.post("/connect/openrouter", json={"key": "bad-key-123"})
    assert r.status_code == 400
    assert not secrets.has("OPENROUTER_API_KEY")
    r = client.post("/connect/openrouter", json={"key": "good-key"})
    assert r.status_code == 200 and secrets.get("OPENROUTER_API_KEY") == "good-key"
    assert client.delete("/connect/openrouter").status_code == 200
    assert not secrets.has("OPENROUTER_API_KEY")


def test_foreign_host_rejected(client):
    assert client.get("/connect/status", headers={"host": "evil.example"}).status_code == 400


# ------------------------------------------------------------- free models
FREE = [m("meta-llama/llama-x-70b:free", 0, 0, NOW - 10, ctx=128000, img=False),
        m("qwen/qwen-x-32b:free", 0, 0, NOW - 20, ctx=64000, img=False),
        m("tiny/tiny-1b:free", 0, 0, NOW - 5, ctx=8000, img=False)]          # too little context: skipped


def test_free_models_added_for_openrouter_and_flagged(isolated_secrets):
    catalog._write_cache("openrouter_models.json", FAKE_CATALOG + FREE)
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    specs = {s["id"]: s for s in catalog.build_specs({"vendors": ["anthropic"]})}
    assert specs["meta-llama/llama-x-70b:free"]["free"] is True
    assert specs["meta-llama/llama-x-70b:free"]["price"] == {"input": 0.0, "output": 0.0}
    assert "tiny/tiny-1b:free" not in specs


def test_free_models_can_be_switched_off(isolated_secrets):
    catalog._write_cache("openrouter_models.json", FAKE_CATALOG + FREE)
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    catalog.set_settings(free_models="off")
    assert not any(s.get("free") for s in catalog.build_specs({"vendors": ["anthropic"]}))


def test_router_uses_free_for_easy_chat_but_not_when_disallowed(isolated_secrets, tmp_path):
    import yaml
    from router.classifier import classify
    from router.registry import Registry
    from router.selector import route
    from router.store import Store
    catalog._write_cache("openrouter_models.json", FAKE_CATALOG + FREE)
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "models.yaml").read_text())
    p = tmp_path / "m.yaml"
    cfg["catalog"]["vendors"] = ["anthropic"]
    p.write_text(yaml.safe_dump(cfg))
    reg, st = Registry(p), Store(tmp_path / "s.db")
    easy = classify("what's a good name for a cat?")
    assert route(reg, st, easy).primary.model.extra["free"] is True
    assert route(reg, st, easy, allow_free=False).primary.model.extra.get("free") is False


def test_settings_endpoint(client):
    r = client.post("/connect/settings", json={"free_models": "all"})
    assert r.status_code == 200 and r.json()["free_models"] == "all"
    assert client.get("/connect/status").json()["settings"]["free_models"] == "all"
    assert client.post("/connect/settings", json={"free_models": "everything"}).status_code == 422


# --------------------------------------------------------- pasting keys
@pytest.mark.parametrize("key,vendor", [
    ("sk-or-v1-abcdef0123456789", "openrouter"), ("sk-ant-api03-abcdef0123456789", "anthropic"),
    ("sk-proj-abcdef0123456789abcdef0123456789", "openai"), ("AIzaSyAbcdef0123456789", "google"),
    ("hello-not-a-key", None)])
def test_detect_vendor(key, vendor):
    assert connect.detect_vendor(key) == vendor


def test_openrouter_key_pasted_in_wrong_row_still_connects(client):
    r = client.post("/connect/anthropic", json={"key": "sk-or-good-key-123"})
    assert r.status_code == 200 and r.json()["vendor"] == "openrouter"
    assert secrets.get("OPENROUTER_API_KEY") == "sk-or-good-key-123" and not secrets.has("ANTHROPIC_API_KEY")


def test_any_key_box_and_quotes_and_bearer_are_cleaned(client):
    r = client.post("/connect/key", json={"key": '  "Bearer sk-or-good-key-123"  '})
    assert r.status_code == 200, r.text
    assert secrets.get("OPENROUTER_API_KEY") == "sk-or-good-key-123"


def test_rejection_explains_why_without_echoing_key(client):
    r = client.post("/connect/key", json={"key": "sk-or-bad-secret-999"})
    d = r.json()["detail"]
    assert r.status_code == 400 and "OpenRouter didn't accept this key" in d["message"]
    assert "HTTP 401" in d["detail"] and "sk-or-bad-secret-999" not in r.text
    assert client.post("/connect/key", json={"key": "not-a-known-key"}).status_code == 400


# ------------------------------------------- Anthropic organization-scoped keys
class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text

    def json(self):
        import json
        return json.loads(self.text)


def _fake_discover(vendor, client=None):
    import httpx
    if not secrets.has("ANTHROPIC_WORKSPACE_ID"):
        body = ('{"type":"error","error":{"type":"invalid_request_error","message":"This API key is not scoped to '
                'a workspace, so this request must include the anthropic-workspace-id header"}}')
        raise httpx.HTTPStatusError("400", request=None, response=_Resp(400, body))
    assert secrets.anthropic_headers()["anthropic-workspace-id"] == secrets.get("ANTHROPIC_WORKSPACE_ID")
    return ["claude-small-9"]


def test_org_key_asks_for_workspace_then_connects(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_native", _fake_discover)
    key = "sk-ant-admin-free-org-key-0000"
    r = client.post("/connect/key", json={"key": key})
    d = r.json()["detail"]
    assert r.status_code == 400 and d["code"] == "needs_workspace" and "wrkspc_" in d["message"]
    assert not secrets.has("ANTHROPIC_API_KEY") and not secrets.has("ANTHROPIC_WORKSPACE_ID")
    bad = client.post("/connect/key", json={"key": key, "workspace_id": "default"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "needs_workspace"
    ok = client.post("/connect/key", json={"key": key, "workspace_id": "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ"})
    assert ok.status_code == 200, ok.text
    assert secrets.get("ANTHROPIC_WORKSPACE_ID") == "wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ"
    assert client.delete("/connect/anthropic").status_code == 200
    assert not secrets.has("ANTHROPIC_WORKSPACE_ID")


def test_single_workspace_is_picked_automatically(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_native", _fake_discover)

    async def one(key):
        return [{"id": "wrkspc_OnlyOneWorkspace1", "name": "Default"}]
    monkeypatch.setattr(connect, "_list_anthropic_workspaces", one)
    r = client.post("/connect/key", json={"key": "sk-ant-api03-org-key-1111111"})
    assert r.status_code == 200 and secrets.get("ANTHROPIC_WORKSPACE_ID") == "wrkspc_OnlyOneWorkspace1"


def test_silly_chat_goes_free_even_against_cheap_fast_paid_model(isolated_secrets, tmp_path):
    """Reproduces the real case: a $0.0004 paid answer was chosen for an everyday question."""
    import yaml
    from router.classifier import classify
    from router.registry import Registry
    from router.selector import route
    from router.store import Store
    catalog._write_cache("openrouter_models.json",
                         [m("openai/gpt-luna-mini", 0.2, 0.8, NOW - 5), m("openai/gpt-luna-pro", 2, 8, NOW - 6),
                          m("openai/gpt-luna-max", 10, 40, NOW - 7)] + FREE)
    secrets.set("OPENROUTER_API_KEY", "sk-or-x")
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "models.yaml").read_text())
    cfg["catalog"]["vendors"] = ["openai"]
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(cfg))
    reg, st = Registry(p), Store(tmp_path / "s.db")
    for q in ("what's the weather at Oudry Dr, Little Elm, TX 75068?", "tell me a joke", "hi there"):
        assert route(reg, st, classify(q)).primary.model.extra["free"] is True, q
    hard = classify("Architect a production-grade distributed rate limiter with security trade-offs and "
                    "concurrency edge cases, with a migration plan")
    assert route(reg, st, hard).primary.model.extra.get("free") is False
    assert route(reg, st, classify("tell me a joke"), prefer="best").primary.model.extra.get("free") is False

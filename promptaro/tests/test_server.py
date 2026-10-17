# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""HTTP API: guard header, chat flow, agent start, models — scripted model."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from conftest import Scripted, act

H = {"x-promptaro": "1"}


@pytest.fixture
def client(router_root):
    from desk import server
    with TestClient(server.app) as c:
        fake = Scripted()
        server.desk.router.providers = {k: fake for k in ("anthropic", "openai", "openai_compatible", "offline")}
        # the router was built from router_root; disable live catalog effects
        c.fake, c.desk = fake, server.desk
        yield c


def wait_status(c, sid, done=("idle", "review"), timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/sessions/{sid}").json()
        if s["session"]["status"] in done and not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_ui_and_health(client):
    assert "Promptaro" in client.get("/").text
    h = client.get("/healthz").json()
    assert h["ok"] and h["ai_available"]


def test_writes_need_app_header(client):
    assert client.post("/api/sessions", json={}).status_code == 403
    assert client.post("/connect/refresh").status_code == 403
    assert client.post("/api/sessions", json={}, headers=H).status_code == 200


def test_event_stream_requires_key(client):
    assert client.get("/api/stream").status_code == 401
    assert client.get("/api/stream?key=nope").status_code == 401
    assert client.post("/api/stream-key").status_code == 403        # still behind the app header
    assert client.post("/api/stream-key", headers=H).json()["key"]


def test_manual_run_still_vets_never_commands(client, repo):
    client.fake.shared = [act("finish", summary="ok")]
    sid = client.post("/api/agents", json={"project": str(repo), "task": "do x"}, headers=H).json()["id"]
    wait_status(client, sid, done=("review",))
    r = client.post(f"/api/sessions/{sid}/run", json={"command": "sudo rm -rf /"}, headers=H)
    assert r.json() == {"ok": False, "blocked": True}
    assert client.post(f"/api/sessions/{sid}/run", json={"command": "echo ok"}, headers=H).json()["ok"]


def test_foreign_host_rejected(client):
    assert client.get("/api/sessions", headers={"host": "evil.example"}).status_code == 400


def test_chat_round_trip_uses_router(client):
    client.fake.shared = ["Paris is the capital of France."]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "capital of France?"}, headers=H).status_code == 200
    s = wait_status(client, sid)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert "Paris" in a["text"] and a["model"] in ("small", "medium", "large") and a["passed"]
    assert s["session"]["title"] == "capital of France?"


def test_agent_api_flow(client, repo):
    client.fake.shared = [act("edit_file", path="calc.py", old="return a - b", new="return a + b"),
                          act("run", command="python -m pytest -q"),
                          act("finish", summary="fixed", tests="passed")]
    r = client.post("/api/agents", json={"project": str(repo), "task": "fix add"}, headers=H)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    wait_status(client, sid, done=("review",))
    d = client.get(f"/api/sessions/{sid}/diff").json()
    assert d["files"][0]["path"] == "calc.py"
    m = client.post(f"/api/sessions/{sid}/merge", headers=H)
    assert m.status_code == 200, m.text
    assert "a + b" in (repo / "calc.py").read_text()


def test_not_a_repo_gets_clear_code(client, tmp_path):
    (tmp_path / "plain").mkdir()
    r = client.post("/api/agents", json={"project": str(tmp_path / "plain"), "task": "do it"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_a_repo"
    assert client.post("/api/repos/init", json={"path": str(tmp_path / "plain")}, headers=H).status_code == 200
    client.fake.shared = [act("finish", summary="ok")]
    assert client.post("/api/agents", json={"project": str(tmp_path / "plain"), "task": "do it"},
                       headers=H).status_code == 200


def test_models_list_for_picker(client):
    ids = [m["id"] for m in client.get("/api/models").json()]
    assert ids == ["small", "medium", "large"]


def test_chat_knows_about_promptaro(client):
    client.fake.shared = ["Click New agent task, pick a git repo, describe the task."]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "why can't I add a new agent?"}, headers=H)
    wait_status(client, sid)
    assert "New agent task" in client.fake.systems[-1] and "git repository" in client.fake.systems[-1]


def test_free_models_for_chat_not_agents(client, repo):
    from router.registry import ModelSpec
    reg = client.desk.router.registry
    reg.models["freebie"] = ModelSpec(
        id="freebie", provider="anthropic", model="f", tier=1, context_window=200000, capabilities={"json"},
        price_in=0, price_out=0, latency_ms_p50=900, prior={k: 0.85 for k in
        ["coding", "research", "writing", "document", "data", "chat"]}, api_key_env="ANTHROPIC_API_KEY",
        extra={"free": True, "trains": True, "name": "Free one", "via": "openrouter (free)"})
    client.fake.shared = ["Hello!"]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    s = wait_status(client, sid)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert a["model"] == "freebie" and a["free"] is True            # default setting: free for chats
    client.fake.shared = [act("finish", summary="ok")]
    client.fake.calls.clear()
    r = client.post("/api/agents", json={"project": str(repo), "task": "rename x"}, headers=H)
    wait_status(client, r.json()["id"], done=("review",))
    assert "freebie" not in client.fake.calls                       # code never goes to a provider that may train
    # a free provider that doesn't train on prompts may do agent work
    reg.models["private-free"] = ModelSpec(
        id="private-free", provider="anthropic", model="p", tier=1, context_window=200000, capabilities={"json"},
        price_in=0, price_out=0, latency_ms_p50=900, prior={k: 0.9 for k in
        ["coding", "research", "writing", "document", "data", "chat"]}, api_key_env="ANTHROPIC_API_KEY",
        extra={"free": True, "trains": False, "name": "Private free", "via": "Groq (free)"})
    client.fake.shared = [act("finish", summary="ok")]
    client.fake.calls.clear()
    r = client.post("/api/agents", json={"project": str(repo), "task": "rename y"}, headers=H)
    wait_status(client, r.json()["id"], done=("review",))
    assert "private-free" in client.fake.calls

# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Subscription model picks, project-bound chats, export, and custom endpoints."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import Scripted, act, wait_task
from desk import workers
from router import catalog

H = {"x-promptaro": "1"}
FAKE = str(Path(__file__).with_name("fake_claude.py"))
HARD = ("Architect and implement a production-grade, thread-safe retry layer with exponential backoff, "
        "jitter, circuit breaking and full test coverage for edge cases and concurrency")


@pytest.fixture
def client(router_root):
    from desk import server
    with TestClient(server.app) as c:
        fake = Scripted()
        server.desk.router.providers = {k: fake for k in ("anthropic", "openai", "openai_compatible", "offline")}
        c.fake, c.desk = fake, server.desk
        yield c


@pytest.fixture
def claude(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMPTARO_CLAUDE_BIN", FAKE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude_call.json"))
    monkeypatch.setattr(workers, "_status_cache", {})
    return tmp_path / "claude_call.json"


def wait_status(c, sid, done=("idle", "review"), timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/sessions/{sid}").json()
        if s["session"]["status"] in done and not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("timed out")


def wait_answer(c, sid, n=1, timeout=10):
    """Wait for the nth assistant event — a settled 'idle' can predate a just-queued turn."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/sessions/{sid}").json()
        if len([e for e in s["events"] if e["kind"] == "assistant"]) >= n and not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("timed out")


def _run(desk, repo, task, **kw):
    async def go():
        s = await desk.start_agent(str(repo), task, prefer=kw.get("prefer", "balanced"), model=kw.get("model", ""),
                                   approval="ask_risky", max_cost=2)
        await wait_task(desk, s["id"])
        return s["id"]
    return asyncio.run(go())


# ------------------------------------------------- subscription model picks
def test_engine_of_maps_variants():
    assert workers.engine_of("claude-code") == ("claude_code", "")
    assert workers.engine_of("claude-code:opus") == ("claude_code", "opus")
    assert workers.engine_of("codex:gpt-5-codex") == ("codex", "gpt-5-codex")
    assert workers.engine_of("gpt-4o") == ("", "")
    assert workers.engine_of("") == ("", "")


def test_models_endpoint_lists_plan_models(client, claude):
    models = client.get("/api/models").json()
    ids = [m["id"] for m in models if m.get("subscription")]
    assert "claude-code" in ids and "claude-code:opus" in ids and "claude-code:sonnet" in ids
    assert not any(i.startswith("codex") for i in ids)                     # codex isn't installed here


def test_claude_variant_reaches_cli(claude, desk_factory, repo):
    desk = desk_factory(Scripted())
    sid = _run(desk, repo, HARD, model="claude-code:opus")
    argv = json.loads(claude.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    ev = desk.store.events(sid)
    assert any(e["kind"] == "model" and e["data"]["name"] == "Claude Opus"
               and e["data"]["subscription"] for e in ev)


def test_fallback_ignores_other_plans_pick(desk_factory):
    """claude-code:opus must not become `codex -m opus` if Codex takes over."""
    run = workers.CodexRun(desk_factory(Scripted()), "x")
    eng, var = workers.engine_of("claude-code:opus")
    run.variant = var if eng == run.engine else ""                        # same line run() uses
    assert run.variant == "" and run.engine == "codex"


def test_chat_pick_of_plan_model_stays_auto(client, claude):
    """Chats can't run on the plan lane (it needs a worktree); picking one falls back to Auto."""
    client.fake.shared = ["plain answer"]
    sid = client.post("/api/sessions", json={"model": "claude-code:opus"}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    s = wait_answer(client, sid, 1)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert a["model"] in ("small", "medium", "large") and not claude.exists()


# --------------------------------------------------------------- project chat
def test_project_chat_reads_but_cannot_write(client, repo):
    client.fake.shared = [act("read_file", path="calc.py"),
                          act("finish", summary="It adds two numbers."),
                          act("write_file", path="evil.py", content="x=1"),
                          act("run", command="rm -rf ."),
                          act("finish", summary="Only reads — nothing changed.")]
    sid = client.post("/api/sessions", json={"project": str(repo)}, headers=H).json()["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "what does calc do?"}, headers=H).status_code == 200
    s = wait_answer(client, sid, 1)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert "adds two numbers" in a["text"]
    assert any(e["kind"] == "tool_result" and e["data"]["action"] == "read_file" and e["data"]["ok"]
               for e in s["events"])
    # second turn tries to write and to run a command — both refused, nothing changed
    client.post(f"/api/sessions/{sid}/messages", json={"text": "now change it"}, headers=H)
    s = wait_answer(client, sid, 2)
    refused = [e for e in s["events"] if e["kind"] == "tool_result" and not e["data"]["ok"]]
    assert len(refused) == 2 and all("read-only" in e["data"]["output"].lower() for e in refused)
    assert not (repo / "evil.py").exists() and "a - b" in (repo / "calc.py").read_text()
    assert [e["kind"] for e in s["events"]][-1] == "assistant"


def test_project_chat_needs_a_real_folder(client):
    assert client.post("/api/sessions", json={"project": "/no/such/folder"}, headers=H).status_code == 400


def test_chat_without_project_is_unchanged(client):
    client.fake.shared = ["plain"]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    wait_status(client, sid)
    assert not client.desk.store.get(sid).get("project")


# ------------------------------------------------------------------- export
def test_export_markdown_and_json(client):
    client.fake.shared = ["Hello there!"]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi there"}, headers=H)
    wait_answer(client, sid, 1)
    md = client.get(f"/api/sessions/{sid}/export")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert md.headers["content-disposition"].endswith('.md"')
    assert "# hi there" in md.text and "## You" in md.text and "Hello there!" in md.text
    j = client.get(f"/api/sessions/{sid}/export?format=json")
    assert j.headers["content-disposition"].endswith('.json"')
    d = j.json()
    assert d["session"]["id"] == sid and any(e["kind"] == "assistant" for e in d["events"])
    assert client.get("/api/sessions/nope/export").status_code == 404


def test_export_includes_project_label(client, repo):
    client.fake.shared = [act("finish", summary="answer")]
    sid = client.post("/api/sessions", json={"project": str(repo)}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    wait_answer(client, sid, 1)
    md = client.get(f"/api/sessions/{sid}/export").text
    assert f"Project: {repo}" in md


# ----------------------------------------------------------- custom endpoints
def test_custom_endpoint_round_trip(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_endpoint_models",
                        lambda base, key_env="", client=None: ["llama3.1:8b", "qwen3:14b"])
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "My Ollama", "base_url": "http://localhost:11434/v1", "private": True})
    assert r.status_code == 200 and r.json()["models"] == 2
    eid = r.json()["endpoint"]["id"]
    models = client.get("/api/models").json()
    got = [m["id"] for m in models if m["id"].startswith(eid)]
    assert sorted(got) == [f"{eid}/llama3.1:8b", f"{eid}/qwen3:14b"]
    ep = client.get("/connect/status").json()["endpoints"][0]
    assert ep["private"] is True and ep["label"] == "My Ollama" and "key_env" not in ep
    assert client.delete(f"/connect/endpoints/{eid}", headers=H).status_code == 200
    assert not [m for m in client.get("/api/models").json() if m["id"].startswith(eid)]
    assert not catalog.custom_endpoints()


def test_custom_endpoint_key_stored_not_returned(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_endpoint_models", lambda base, key_env="", client=None: ["m1"])
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "Proxy", "base_url": "https://proxy.example.com/v1", "key": "test-key-12345"})
    assert r.status_code == 200 and r.json()["endpoint"]["has_key"]
    assert "test-key-12345" not in client.get("/connect/status").text       # the key never leaves storage


def test_custom_endpoint_unreachable_without_models(client, monkeypatch):
    def boom(base, key_env="", client=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(catalog, "discover_endpoint_models", boom)
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "Down", "base_url": "http://localhost:9/v1"})
    assert r.status_code == 400 and "Couldn't reach" in r.json()["detail"]
    # typing the models by hand still works even when /models won't answer
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "Down", "base_url": "http://localhost:9/v1", "models": "m1, m2"})
    assert r.status_code == 200 and r.json()["models"] == 2


def test_custom_endpoint_bad_url(client):
    assert client.post("/connect/endpoints", headers=H,
                       json={"label": "x", "base_url": "localhost:1234"}).status_code == 400

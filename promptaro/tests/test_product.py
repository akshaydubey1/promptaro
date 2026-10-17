# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""ChatGPT plan via Codex, plan hand-over, streaming, explanations, feedback, savings. Author: Akshay Dubey"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import Scripted, act, wait_task
from desk import workers
from test_server import H, client, wait_status  # noqa: F401  (fixture)

FAKE_CODEX = str(Path(__file__).with_name("fake_codex.py"))
FAKE_CLAUDE = str(Path(__file__).with_name("fake_claude.py"))
HARD = ("Architect and implement a production-grade, thread-safe retry layer with exponential backoff, "
        "jitter, circuit breaking and full test coverage for edge cases and concurrency")


@pytest.fixture
def codex(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMPTARO_CODEX_BIN", FAKE_CODEX)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex_call.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-passed")
    return tmp_path / "codex_call.json"


@pytest.fixture
def claude(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMPTARO_CLAUDE_BIN", FAKE_CLAUDE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude_call.json"))
    return tmp_path / "claude_call.json"


def _run(desk, repo, task, model=""):
    async def go():
        s = await desk.start_agent(str(repo), task, prefer="balanced", model=model, approval="ask_risky", max_cost=2)
        await wait_task(desk, s["id"])
        return s["id"]
    return asyncio.run(go())


@pytest.mark.parametrize("mode,sub", [("chatgpt", True), ("api", False), ("none", False)])
def test_detects_chatgpt_plan(codex, monkeypatch, mode, sub):
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    st = asyncio.run(workers.codex_status(force=True))
    assert st["installed"] and st["subscription"] is sub


def test_hard_task_runs_on_chatgpt_plan(codex, desk_factory, repo):
    fake = Scripted()
    desk = desk_factory(fake)
    sid = _run(desk, repo, HARD)
    s, ev = desk.store.get(sid), desk.store.events(sid)
    assert fake.calls == []
    m = next(e["data"] for e in ev if e["kind"] == "model")
    assert m["subscription"] and m["name"] == "Codex" and "ChatGPT" in m["via"]
    assert [e["data"]["action"] for e in ev if e["kind"] == "tool_call"] == ["edit_file", "run"]
    assert all(e["data"]["ok"] for e in ev if e["kind"] == "tool_result")
    assert ev[-1]["kind"] == "finish" and "1 test passes" in ev[-1]["data"]["summary"]
    assert "a + b" in Path(s["worktree"], "calc.py").read_text()
    assert s["cost"] == 0 and s["tokens_in"] == 3000 and s["state"]["codex_session"] == "thread-9"
    call = json.loads(codex.read_text())
    assert call["has_api_key"] is False and 'sandbox_mode="workspace-write"' in call["argv"]
    assert desk.store.totals()["subscription_runs"] == 1 and desk.store.totals()["saved"] > 0
    # a follow-up resumes the same Codex conversation
    async def follow_up():
        desk.message_agent(sid, "also add a docstring")
        await wait_task(desk, sid)
    asyncio.run(follow_up())
    assert json.loads(codex.read_text())["argv"][:2] == ["exec", "resume"]


def test_claude_limit_hands_over_to_chatgpt_plan(claude, codex, monkeypatch, desk_factory, repo):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")                       # Claude plan: usage limit reached
    fake = Scripted()
    desk = desk_factory(fake)
    sid = _run(desk, repo, HARD)
    ev = desk.store.events(sid)
    names = [e["data"]["name"] for e in ev if e["kind"] == "model"]
    assert names == ["Claude Code", "Codex"] and fake.calls == []
    assert any("resting for an hour" in e["data"].get("text", "") for e in ev if e["kind"] == "note")
    assert workers.resting("claude_code") > 3000
    # next hard task goes straight to the plan that has room
    sid2 = _run(desk, repo, HARD)
    assert [e["data"]["name"] for e in desk.store.events(sid2) if e["kind"] == "model"] == ["Codex"]


def test_both_plans_limited_falls_back_to_router(claude, codex, monkeypatch, desk_factory, repo):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    monkeypatch.setenv("FAKE_CODEX_FAIL", "1")
    fake = Scripted([act("finish", summary="done by router")])
    desk = desk_factory(fake)
    sid = _run(desk, repo, HARD)
    assert fake.calls and desk.store.events(sid)[-1]["data"]["summary"] == "done by router"


def test_pick_codex_from_model_menu(claude, codex, desk_factory, repo):
    desk = desk_factory(Scripted())
    sid = _run(desk, repo, "rename x to y", model="codex")             # easy, but the user asked for it
    assert [e["data"]["name"] for e in desk.store.events(sid) if e["kind"] == "model"] == ["Codex"]


# ------------------------------------------------------------- chat features
def test_chat_streams_and_explains_and_counts_savings(client):
    client.fake.shared = ["Here's one: why did the developer go broke? Because he used up all his cache."]
    q = client.desk.store.subscribe("*")
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "tell me a joke"}, headers=H)
    s = wait_status(client, sid)
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    deltas = [e for e in seen if e["kind"] == "delta"]
    assert deltas and "cache" in "".join(d["data"]["text"] for d in deltas)
    assert not any(e["kind"] == "delta" for e in s["events"])        # streamed text isn't stored twice
    a = next(e for e in s["events"] if e["kind"] == "assistant")["data"]
    x = a["explain"]
    assert x["level"] == "easy" and any(o["chosen"] for o in x["options"]) and x["because"]
    assert a["model"] == "small" and x["saved_usd"] > 0              # vs the strongest model, same tokens
    ins = client.get("/api/insights").json()
    assert ins["totals"]["answers"] == 1 and ins["totals"]["saved"] > 0
    assert set(ins["subscriptions"]) == {"claude_code", "codex"}


def test_thumbs_down_teaches_router_and_retry_uses_stronger_model(client):
    client.fake.shared = ["meh answer"]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "write a haiku about rain"}, headers=H)
    s = wait_status(client, sid)
    a = next(e for e in s["events"] if e["kind"] == "assistant")
    st = client.desk.router.store
    before = st.expected_quality(a["data"]["model"], a["data"]["task_type"], a["data"]["complexity"], 0.8)
    r = client.post(f"/api/sessions/{sid}/feedback", json={"seq": a["seq"], "good": False}, headers=H)
    assert r.status_code == 200 and r.json()["can_retry"]
    assert st.expected_quality(a["data"]["model"], a["data"]["task_type"], a["data"]["complexity"], 0.8) < before
    client.fake.shared = ["Soft rain on the roof / ..."]
    assert client.post(f"/api/sessions/{sid}/retry", json={"seq": a["seq"]}, headers=H).status_code == 200
    time = __import__("time")
    for _ in range(100):
        evs = client.get(f"/api/sessions/{sid}").json()["events"]
        answers = [e for e in evs if e["kind"] == "assistant"]
        if len(answers) == 2:
            break
        time.sleep(0.05)
    tier = {"small": 1, "medium": 2, "large": 3}
    assert answers[1]["data"]["retry"] and tier[answers[1]["data"]["model"]] > tier[answers[0]["data"]["model"]]
    assert [e["kind"] for e in evs].count("user") == 1                # the question isn't repeated
    assert any(e["kind"] == "feedback" and e["data"]["good"] is False for e in evs)
    assert client.post(f"/api/sessions/{sid}/feedback", json={"seq": 999999, "good": True}, headers=H).status_code == 404


def test_merging_agent_work_counts_as_success(client, repo):
    client.fake.shared = [act("edit_file", path="calc.py", old="return a - b", new="return a + b"),
                          act("finish", summary="fixed", tests="passed")]
    sid = client.post("/api/agents", json={"project": str(repo), "task": "fix add"}, headers=H).json()["id"]
    wait_status(client, sid, done=("review",))
    assert client.post(f"/api/sessions/{sid}/merge", headers=H).status_code == 200
    learned = client.get("/api/insights").json()["learned"]
    assert any(x["thumbs_up"] >= 1 for x in learned)
    step = next(e for e in client.get(f"/api/sessions/{sid}").json()["events"] if e["kind"] == "model")
    assert step["data"]["explain"]["because"]


# ------------------------------------------------------------- coding tools
def test_gateway_is_mounted_and_token_protected(client):
    assert client.post("/v1/messages", json={"model": "x", "messages": []}).status_code == 401
    assert client.post("/api/gateway/token").status_code == 403            # web pages can't read it
    tok = client.post("/api/gateway/token", headers=H).json()["token"]
    assert tok.startswith("pro-")
    models = client.get("/v1/models", headers={"authorization": f"Bearer {tok}"}).json()
    assert models["data"][0]["id"] == "promptaro-auto"
    client.post("/api/gateway/rotate", headers=H)
    assert client.post("/api/gateway/token", headers=H).json()["token"] != tok
    info = client.get("/api/gateway").json()
    assert info["spend"] == "free" and info["openai_base_url"].endswith("/v1")


def test_gateway_usage_shows_in_savings(client):
    spec = client.desk.router.registry.models["small"]
    client.desk.router.notify_usage(spec, 1000, 200)
    t = client.get("/api/insights").json()["totals"]
    assert t["gateway_requests"] == 1 and t["saved"] > 0
    assert client.get("/api/gateway").json()["recent"][0]["model"] == "small"


def test_install_launchers(client, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home2"))
    r = client.post("/api/gateway/launchers", headers=H).json()
    names = sorted(Path(f).name for f in r["files"])
    assert names == ["promptaro-aider", "promptaro-claude", "promptaro-env", "tw-aider", "tw-claude", "tw-env"]
    for f in r["files"]:
        assert "pro-" + "x" not in Path(f).read_text()            # the token is fetched at run time, never stored
    claude = (tmp_path / "home2" / ".local" / "bin" / "promptaro-claude").read_text()
    assert "ANTHROPIC_AUTH_TOKEN" in claude and "api/gateway/token" in claude and "Akshay Dubey" in claude
    import os
    assert os.access(tmp_path / "home2" / ".local" / "bin" / "promptaro-claude", os.X_OK)
    assert "promptaro-claude" in (tmp_path / "home2" / ".local" / "bin" / "tw-claude").read_text()  # legacy shim


def test_free_status_lists_providers(client):
    d = client.get("/api/free").json()
    ids = {p["id"] for p in d["providers"]}
    assert {"groq", "cerebras", "nvidia", "mistral"} <= ids
    assert next(p for p in d["providers"] if p["id"] == "mistral")["trains"] is True

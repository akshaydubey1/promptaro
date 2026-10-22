# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Hard tasks go to Claude Code on the user's subscription; everything else stays on the router."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import Scripted, act, fake_cli, wait_task
from desk import workers

FAKE = str(Path(__file__).with_name("fake_claude.py"))
HARD = ("Architect and implement a production-grade, thread-safe retry layer with exponential backoff, "
        "jitter, circuit breaking and full test coverage for edge cases and concurrency")


@pytest.fixture
def claude(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMPTARO_CLAUDE_BIN", fake_cli(Path(FAKE), tmp_path, "fake_claude"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude_call.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-passed")
    monkeypatch.setattr(workers, "_status_cache", {})
    return tmp_path / "claude_call.json"


@pytest.mark.parametrize("mode,sub", [("sub", True), ("api", False), ("none", False)])
def test_detects_subscription(claude, monkeypatch, mode, sub):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    st = asyncio.run(workers.claude_status(force=True))
    assert st["installed"] and st["subscription"] is sub


def test_not_installed(monkeypatch):
    monkeypatch.setenv("PROMPTARO_CLAUDE_BIN", "/nope/claude")
    monkeypatch.setattr(workers, "_status_cache", {})
    assert asyncio.run(workers.claude_status(force=True))["installed"] is False


def _run(desk, repo, task, **kw):
    async def go():
        s = await desk.start_agent(str(repo), task, prefer=kw.get("prefer", "balanced"), model=kw.get("model", ""),
                                   approval="ask_risky", max_cost=2)
        await wait_task(desk, s["id"])
        return s["id"]
    return asyncio.run(go())


def test_hard_task_runs_on_subscription(claude, desk_factory, repo):
    fake = Scripted()
    desk = desk_factory(fake)
    sid = _run(desk, repo, HARD)
    s = desk.store.get(sid)
    ev = desk.store.events(sid)
    assert fake.calls == []                                            # no API model was used
    assert any(e["kind"] == "model" and e["data"].get("subscription") for e in ev)
    assert any(e["kind"] == "tool_call" and e["data"]["action"] == "edit_file" for e in ev)
    assert ev[-1]["kind"] == "finish" and ev[-1]["data"]["subscription"]
    assert "a + b" in Path(s["worktree"], "calc.py").read_text() and "a - b" in (repo / "calc.py").read_text()
    assert s["cost"] == 0 and s["tokens_in"] == 2000 and s["tokens_out"] == 300
    call = json.loads(claude.read_text())
    assert call["has_api_key"] is False                               # billed to the plan, not an API key
    assert "--allowedTools" in call["argv"] and "Bash(git push:*)" in call["argv"]
    assert s["state"]["claude_session"] == "sess-123"


def test_easy_task_stays_on_router(claude, desk_factory, repo):
    fake = Scripted([act("finish", summary="renamed")])
    desk = desk_factory(fake)
    _run(desk, repo, "rename variable x to y")
    assert fake.calls and not claude.exists()


def test_falls_back_when_claude_code_fails(claude, monkeypatch, desk_factory, repo):
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    fake = Scripted([act("finish", summary="done by router")])
    desk = desk_factory(fake)
    sid = _run(desk, repo, HARD)
    ev = desk.store.events(sid)
    assert any("usage limit" in e["data"].get("text", "") for e in ev if e["kind"] == "note")
    assert fake.calls and ev[-1]["data"]["summary"] == "done by router"


def test_not_signed_in_uses_router(claude, monkeypatch, desk_factory, repo):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "none")
    fake = Scripted([act("finish", summary="router")])
    desk = desk_factory(fake)
    _run(desk, repo, HARD)
    assert fake.calls and not claude.exists()

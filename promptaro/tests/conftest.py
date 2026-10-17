# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Fixtures: isolated homes, a fixed model list, a scripted model, a temp git repo."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("PROMPTARO_NO_MIGRATE", "1")   # never move real ~/.tillwright in tests

HERE = Path(__file__).resolve().parent
ROUTER = Path(os.environ.get("SMART_ROUTER_ROOT", HERE.parent.parent / "smart-router")).resolve()
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(HERE.parent))

from router import catalog, secrets  # noqa: E402
from router.providers import Completion, Provider  # noqa: E402

MODELS = [
    {"id": "small", "provider": "anthropic", "model": "s", "api_key_env": "ANTHROPIC_API_KEY", "tier": 1,
     "context_window": 200000, "capabilities": ["json"], "price": {"input": 1, "output": 5},
     "prior": {k: 0.8 for k in ["coding", "research", "writing", "document", "data", "chat"]}},
    {"id": "medium", "provider": "anthropic", "model": "m", "api_key_env": "ANTHROPIC_API_KEY", "tier": 2,
     "context_window": 200000, "capabilities": ["json", "long_context"], "price": {"input": 3, "output": 15},
     "prior": {k: 0.9 for k in ["coding", "research", "writing", "document", "data", "chat"]}},
    {"id": "large", "provider": "anthropic", "model": "l", "api_key_env": "ANTHROPIC_API_KEY", "tier": 3,
     "context_window": 200000, "capabilities": ["json", "long_context"], "price": {"input": 15, "output": 75},
     "prior": {k: 0.95 for k in ["coding", "research", "writing", "document", "data", "chat"]}},
]


class Scripted(Provider):
    """Replies from a per-model queue (falls back to a shared queue)."""

    def __init__(self, shared=None, per_model=None):
        self.shared = list(shared or [])
        self.per = {k: list(v) for k, v in (per_model or {}).items()}
        self.calls = []
        self.systems = []

    async def complete(self, spec, system, user, max_tokens, **kw):
        self.calls.append(spec.id)
        self.systems.append(system)
        q = self.per.get(spec.id) or self.shared
        r = q.pop(0) if q else '```json\n{"action":"finish","args":{"summary":"nothing left","tests":"not_run"}}\n```'
        if isinstance(r, dict):
            r = "```json\n" + json.dumps(r) + "\n```"
        return Completion(r, 1000, 100, 3.0, "end_turn")


def act(action, thought="", **args):
    return {"thought": thought, "action": action, "args": args}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    from desk import gitops
    home = tmp_path / "home"
    monkeypatch.setattr(secrets, "_kr_ok", False)
    monkeypatch.setattr(secrets, "_FALLBACK", home / "creds.json")
    monkeypatch.setattr(catalog, "HOME", home)
    monkeypatch.setattr(catalog, "ollama_models", lambda client=None: [])
    monkeypatch.setattr(catalog, "FREE", catalog.FreeQuota())       # fresh free allowance per test

    async def _no_network(self, client, force=False):
        return None
    monkeypatch.setattr(catalog.FreeQuota, "refresh", _no_network)
    from router import gateway, quota
    monkeypatch.setattr(quota, "LEDGER", quota.Ledger())
    monkeypatch.setattr(quota, "use", lambda path: quota.LEDGER)
    monkeypatch.setattr(gateway, "_client", None)
    monkeypatch.setattr(gitops, "HOME", home / "desk")
    from desk import workers                    # ignore any real Claude Code on the test machine
    for n in ("PROMPTARO_CLAUDE_BIN", "TILLWRIGHT_CLAUDE_BIN"):
        monkeypatch.setenv(n, str(home / "no-claude"))
    for n in ("PROMPTARO_CODEX_BIN", "TILLWRIGHT_CODEX_BIN"):
        monkeypatch.setenv(n, str(home / "no-codex"))
    monkeypatch.setattr(workers, "_status_cache", {})
    monkeypatch.setattr(workers, "_rest_until", {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("PROMPTARO_HOME", str(home / "desk"))
    monkeypatch.setenv("TILLWRIGHT_HOME", str(home / "desk"))
    monkeypatch.setenv("PROMPTARO_DB", str(home / "desk.db"))
    monkeypatch.setenv("TILLWRIGHT_DB", str(home / "desk.db"))
    monkeypatch.setenv("ROUTER_DB", str(home / "router.db"))
    # Make `python` / `python3` in agent commands resolve to this interpreter (with pytest).
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    for k in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from desk import legal
    legal.accept()                    # tests run as a user who already accepted the terms
    return home


@pytest.fixture
def router_root(tmp_path, monkeypatch):
    root = tmp_path / "sr"
    (root / "config").mkdir(parents=True)
    shutil.copytree(ROUTER / "skills", root / "skills")
    cfg = yaml.safe_load((ROUTER / "config" / "models.yaml").read_text())
    cfg["catalog"]["enabled"] = False
    cfg["models"] = MODELS + cfg["models"]
    (root / "config" / "models.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("SMART_ROUTER_ROOT", str(root))
    return root


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    sh("git init -q -b main && git -c user.name=t -c user.email=t@t add -A && "
       "git -c user.name=t -c user.email=t@t commit -qm init", r)
    return r


@pytest.fixture
def desk_factory(router_root, tmp_path):
    from router.engine import Router
    from desk.desk import Desk
    from desk.store import Store

    def make(provider):
        r = Router(router_root, db_path=tmp_path / "r.db", use_catalog=False,
                   providers={"anthropic": provider, "openai": provider, "openai_compatible": provider,
                              "offline": provider})
        return Desk(r, Store(tmp_path / "d.db"))
    return make


def drain_catalog_refresh(c):
    """Wait out the app's startup registry swap (lifespan -> refresh_catalog -> reload).

    Tests that inject ModelSpecs into router.registry race that swap: if reload lands
    after the injection, the chat runs on the fresh registry and the added model is gone.
    The swap is correct in production; here we just settle it before tests begin."""
    t = getattr(c.app.state, "catalog_refresh", None)
    if t is None or t.done():
        return

    async def _wait():
        try:
            await asyncio.wait_for(t, 10)
        except Exception:
            pass

    c.portal.call(_wait)


def wait_task(desk, sid, timeout=30):
    async def _w():
        t = desk.tasks.get(sid)
        if t:
            await asyncio.wait_for(asyncio.shield(t), timeout)
    return _w()

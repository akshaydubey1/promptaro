# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Shared fixtures: isolated secret store + a fixed model list (no network)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("PROMPTARO_NO_MIGRATE", "1")   # never move real ~/.akshay-router in tests
# Most tests exercise routing across paid models; tests of the real default (paid off) unset this.
os.environ.setdefault("PROMPTARO_ALLOW_PAID", "1")

from router import catalog, secrets  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

STATIC_MODELS = [
    {"id": "claude-haiku", "provider": "anthropic", "model": "claude-haiku-x", "api_key_env": "ANTHROPIC_API_KEY",
     "tier": 1, "context_window": 200000, "capabilities": ["json", "tools", "vision", "web"],
     "price": {"input": 1.0, "output": 5.0}, "latency_ms_p50": 900,
     "prior": {"coding": 0.72, "research": 0.65, "writing": 0.74, "document": 0.70, "data": 0.68, "chat": 0.80}},
    {"id": "claude-sonnet", "provider": "anthropic", "model": "claude-sonnet-x", "api_key_env": "ANTHROPIC_API_KEY",
     "tier": 2, "context_window": 200000, "capabilities": ["json", "tools", "vision", "long_context", "web"],
     "price": {"input": 3.0, "output": 15.0}, "latency_ms_p50": 1800,
     "prior": {"coding": 0.88, "research": 0.84, "writing": 0.86, "document": 0.85, "data": 0.84, "chat": 0.86}},
    {"id": "claude-opus", "provider": "anthropic", "model": "claude-opus-x", "api_key_env": "ANTHROPIC_API_KEY",
     "tier": 3, "context_window": 200000, "capabilities": ["json", "tools", "vision", "long_context", "web"],
     "price": {"input": 15.0, "output": 75.0}, "latency_ms_p50": 3500,
     "prior": {"coding": 0.94, "research": 0.91, "writing": 0.90, "document": 0.90, "data": 0.90, "chat": 0.88}},
]


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(secrets, "_kr_ok", False)
    monkeypatch.setattr(secrets, "_FALLBACK", home / "credentials.json")
    monkeypatch.setattr(catalog, "HOME", home)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(catalog, "ollama_models", lambda client=None: [])
    monkeypatch.setattr(catalog, "FREE", catalog.FreeQuota())       # fresh free allowance per test

    async def _no_network(self, client, force=False):
        return None
    monkeypatch.setattr(catalog.FreeQuota, "refresh", _no_network)
    from router import gateway, quota
    monkeypatch.setattr(quota, "LEDGER", quota.Ledger())             # fresh free-tier ledger per test
    monkeypatch.setattr(quota, "use", lambda path: quota.LEDGER)
    monkeypatch.setattr(gateway, "_client", None)
    monkeypatch.setattr(gateway, "_sticky", {})                      # fresh session stickiness per test
    return home


@pytest.fixture
def test_root(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copytree(REPO / "skills", root / "skills")
    cfg = yaml.safe_load((REPO / "config" / "models.yaml").read_text())
    cfg["catalog"]["enabled"] = False
    cfg["models"] = STATIC_MODELS + cfg["models"]
    (root / "config" / "models.yaml").write_text(yaml.safe_dump(cfg))
    return root

# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Tillwright → Promptaro migration: old homes, keys, headers, model aliases."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from router import migrate, secrets
from test_server import H, client  # noqa: F401  (fixture)


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """Point the legacy homes at temp dirs and re-enable the migration."""
    tw = tmp_path / "old-tw"; ak = tmp_path / "old-ak"; new = tmp_path / "new"
    tw.mkdir(); ak.mkdir()
    monkeypatch.setenv("TILLWRIGHT_LEGACY_HOME", str(tw))
    monkeypatch.setenv("AKSHAY_ROUTER_LEGACY_HOME", str(ak))
    monkeypatch.delenv("PROMPTARO_NO_MIGRATE", raising=False)
    monkeypatch.setattr(migrate, "_done", False)
    return tw, ak, new


def _desk_db(path: Path, worktree: str) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE sessions (id TEXT, worktree TEXT)")
    con.execute("INSERT INTO sessions VALUES (?, ?)", ("s1", worktree))
    con.commit(); con.close()


def test_legacy_dirs_move_and_desk_db_paths_rewritten(legacy):
    tw, ak, new = legacy
    _desk_db(tw / "desk.db", f"{tw}/worktrees/proj-wt")
    (tw / "worktrees").mkdir(); (tw / "worktrees" / "proj-wt").mkdir()
    (ak / "credentials.json").write_text('{"OPENROUTER_API_KEY": "k"}')
    (ak / "ledger.json").write_text("{}")

    migrate.migrate(new)

    assert (new / "desk.db").exists() and (new / "ledger.json").exists()
    assert (new / "credentials.json").exists() and (new / "worktrees" / "proj-wt").is_dir()
    con = sqlite3.connect(str(new / "desk.db"))
    assert con.execute("SELECT worktree FROM sessions").fetchone()[0] == f"{new}/worktrees/proj-wt"
    con.close()
    assert not tw.exists()                       # emptied dirs are removed
    assert (new / ".migrated").exists()


def test_migration_never_clobbers_newer_data(legacy):
    tw, ak, new = legacy
    new.mkdir()
    (new / "ledger.json").write_text('{"new": 1}')
    (ak / "ledger.json").write_text('{"old": 1}')
    migrate.migrate(new)
    assert '"new"' in (new / "ledger.json").read_text()
    assert ak.exists()                           # leftover kept, not deleted


def test_migration_runs_once(legacy):
    tw, ak, new = legacy
    (ak / "f").write_text("1")
    migrate.migrate(new)
    ak.mkdir(exist_ok=True)                      # emptied dir was removed; recreate for a late file
    (ak / "late").write_text("2")                # new file after first run
    migrate.migrate(new)
    assert not (new / "late").exists() and (ak / "late").exists()


def test_secrets_read_legacy_file_fallback(tmp_path, monkeypatch):
    old = tmp_path / "old" / "credentials.json"
    old.parent.mkdir()
    old.write_text('{"ANTHROPIC_API_KEY": "sk-old"}')
    monkeypatch.setattr(secrets, "_kr_ok", False)
    monkeypatch.setattr(secrets, "_FALLBACK", tmp_path / "new" / "credentials.json")
    monkeypatch.setattr(secrets, "_LEGACY_FALLBACK", old)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert secrets.get("ANTHROPIC_API_KEY") == "sk-old"


def test_secrets_legacy_file_cleared_on_delete(tmp_path, monkeypatch):
    new_f = tmp_path / "new" / "credentials.json"
    new_f.parent.mkdir(); new_f.write_text('{"K": "v"}')
    monkeypatch.setattr(secrets, "_kr_ok", False)
    monkeypatch.setattr(secrets, "_FALLBACK", new_f)
    monkeypatch.setattr(secrets, "_LEGACY_FALLBACK", tmp_path / "old" / "credentials.json")
    secrets.delete("K")
    assert not secrets.get("K")


def test_legacy_app_header_still_accepted(client):
    assert client.post("/api/gateway/rotate", headers={"x-tillwright": "1"}).status_code == 200
    assert client.post("/api/gateway/rotate", headers=H).status_code == 200
    assert client.post("/api/gateway/rotate").status_code == 403


def test_legacy_model_aliases_still_advertised(client):
    tok = client.post("/api/gateway/token", headers=H).json()["token"]
    ids = {m["id"] for m in client.get("/v1/models", headers={"authorization": f"Bearer {tok}"}).json()["data"]}
    for alias in ("tillwright-auto", "tillwright-fast", "tillwright-best",
                  "promptaro-auto", "promptaro-fast", "promptaro-best"):
        assert alias in ids


def test_promptaro_launchers_plus_legacy_shims(client, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    files = {Path(f).stem for f in client.post("/api/gateway/launchers", headers=H).json()["files"]}
    assert {"promptaro-claude", "promptaro-aider", "promptaro-env"} <= files
    assert {"tw-claude", "tw-aider", "tw-env"} <= files          # compat shims installed too


def test_single_legacy_env_targets_that_home(legacy, monkeypatch):
    """Only TILLWRIGHT_HOME set: the resolved home IS that dir, so desk data migrates there
    rather than splitting between ~/.promptaro and the env dir."""
    tw, ak, new = legacy
    monkeypatch.delenv("PROMPTARO_HOME", raising=False)
    monkeypatch.setenv("TILLWRIGHT_HOME", str(new))
    (ak / "ledger.json").write_text("{}")
    (tw / "desk.db").write_text("")
    assert migrate.home() == new
    assert (new / "ledger.json").exists() and (new / "desk.db").exists()


def test_keychain_present_but_empty_falls_through_to_file(tmp_path, monkeypatch):
    """A Tillwright-era file-backend user on a machine where keyring now works still finds keys."""
    class EmptyKR:
        @staticmethod
        def get_password(service, name): return None
    monkeypatch.setattr(secrets, "_kr_ok", True)
    monkeypatch.setattr(secrets, "keyring", EmptyKR)
    f = tmp_path / "credentials.json"
    f.write_text('{"ANTHROPIC_API_KEY": "sk-file"}')
    monkeypatch.setattr(secrets, "_FALLBACK", f)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert secrets.get("ANTHROPIC_API_KEY") == "sk-file"

def test_migrated_worktrees_get_git_back_pointer_repaired(legacy, tmp_path):
    """A worktree that moved with the home dir must have its gitdir back-pointer repointed —
    otherwise git treats it as deleted and `worktree prune` can drop unmerged work."""
    tw, ak, new = legacy
    repo = tmp_path / "repo"
    gdir = repo / ".git" / "worktrees" / "proj-wt"
    gdir.mkdir(parents=True)
    old_wt = tw / "worktrees" / "proj-wt"
    old_wt.mkdir(parents=True)
    (old_wt / ".git").write_text(f"gitdir: {gdir}\n")          # worktree → repo (unchanged by move)
    (gdir / "gitdir").write_text(f"{old_wt}/.git\n")          # repo → old worktree path (goes stale)

    migrate.migrate(new)

    moved = new / "worktrees" / "proj-wt"
    assert moved.is_dir()
    assert (gdir / "gitdir").read_text().strip() == str(moved / ".git")

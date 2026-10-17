# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""One-time Tillwright → Promptaro data migration. Author: Akshay Dubey

Old installs kept user data in two places: ``~/.akshay-router`` (credentials,
quota ledger, settings, model catalog cache) and ``~/.tillwright`` (sessions,
worktrees, logs). Both move to ``~/.promptaro``. The migration:

  * moves every entry of each legacy dir into the new home (same-filesystem
    renames — fast even for large worktrees), skipping anything already there;
  * rewrites ``worktree`` paths inside desk.db so existing agent sessions keep
    pointing at their copies;
  * leaves non-empty leftovers alone and never fails startup.

It runs once per process, on the first touch of the home directory.
``PROMPTARO_NO_MIGRATE=1`` disables it (used by the test suite). The legacy
locations can be pointed elsewhere with ``TILLWRIGHT_LEGACY_HOME`` and
``AKSHAY_ROUTER_LEGACY_HOME``, mainly for tests.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

_LEGACY_ENVS = {"~/.tillwright": "TILLWRIGHT_LEGACY_HOME", "~/.akshay-router": "AKSHAY_ROUTER_LEGACY_HOME"}
_done = False
_lock = threading.Lock()


def legacy_dirs() -> list[Path]:
    return [Path(os.environ.get(env) or d).expanduser() for d, env in _LEGACY_ENVS.items()]


_HOME_ENVS = ("PROMPTARO_HOME", "TILLWRIGHT_HOME", "ROUTER_HOME")


def home_path() -> Path:
    """Resolve the Promptaro data directory without migrating. One canonical order
    everywhere: PROMPTARO_HOME > TILLWRIGHT_HOME > ROUTER_HOME > ~/.promptaro."""
    return Path(next((os.environ[e] for e in _HOME_ENVS if os.environ.get(e)), None)
                or Path.home() / ".promptaro")


def home() -> Path:
    """The Promptaro data directory, migrating legacy installs on first use."""
    p = home_path()
    migrate(p)
    return p


def migrate(new: Path) -> None:
    global _done
    if _done or os.environ.get("PROMPTARO_NO_MIGRATE"):
        return
    with _lock:
        if _done:
            return
        _done = True
        if new.exists() and (new / ".migrated").exists():
            return
        moved_db: Path | None = None
        for old in legacy_dirs():
            if old == new or not old.is_dir():
                continue
            try:
                new.mkdir(parents=True, exist_ok=True)
                for entry in old.iterdir():
                    dest = new / entry.name
                    if dest.exists():
                        continue                        # never clobber newer data
                    try:
                        os.rename(entry, dest)
                        if entry.name == "desk.db":
                            moved_db = dest
                    except OSError:
                        continue                        # different filesystem: leave, user still has it
                try:
                    old.rmdir()                         # only goes away if fully emptied
                except OSError:
                    pass
            except OSError:
                continue
        if moved_db:
            _rewrite_worktree_paths(moved_db, new)
        try:
            (new / ".migrated").touch()
        except OSError:
            pass


def _rewrite_worktree_paths(db: Path, new: Path) -> None:
    """Session rows keep absolute worktree paths; point them at the new home."""
    try:
        con = sqlite3.connect(str(db))
        for old in legacy_dirs():
            con.execute("UPDATE sessions SET worktree=? || substr(worktree, ?) "
                        "WHERE substr(worktree, 1, ?) = ?",
                        (str(new), len(str(old)) + 1, len(str(old)) + 1, str(old) + "/"))
        con.commit()
        con.close()
    except sqlite3.Error:
        pass

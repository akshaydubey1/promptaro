# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Sessions and their event streams, in SQLite, with live fan-out. Author: Akshay Dubey"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
          id TEXT PRIMARY KEY, kind TEXT, title TEXT, status TEXT, created REAL, updated REAL,
          project TEXT, repo TEXT, worktree TEXT, branch TEXT, base_branch TEXT, base_sha TEXT,
          prefer TEXT, model TEXT, approval TEXT, cost REAL DEFAULT 0, tokens_in INTEGER DEFAULT 0,
          tokens_out INTEGER DEFAULT 0, state TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL, kind TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS events_by_session ON events(session_id, seq);
        CREATE TABLE IF NOT EXISTS totals (name TEXT PRIMARY KEY, value REAL DEFAULT 0);
        """)
        # Anything that was running when the app last closed is no longer running.
        self.db.execute("UPDATE sessions SET status='stopped' WHERE status IN ('running','waiting')")
        self._subs: dict[str, set[asyncio.Queue]] = {}

    # ----------------------------------------------------------- sessions
    def create(self, kind: str, title: str, **fields) -> dict:
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        row = {"id": sid, "kind": kind, "title": title[:120], "status": "idle", "created": now, "updated": now,
               "prefer": "balanced", "model": "", "approval": "ask_risky", **fields}
        if not isinstance(row.get("state", "{}"), str):
            row["state"] = json.dumps(row["state"])
        cols = ",".join(row)
        with self.lock:
            self.db.execute(f"INSERT INTO sessions({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        return self.get(sid)

    def get(self, sid: str) -> dict | None:
        r = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["state"] = json.loads(d.get("state") or "{}")
        return d

    def update(self, sid: str, **fields) -> None:
        if "state" in fields and not isinstance(fields["state"], str):
            fields["state"] = json.dumps(fields["state"])
        fields["updated"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE sessions SET {sets} WHERE id=?", [*fields.values(), sid])
        if "status" in fields or "title" in fields:          # live sidebar updates (not persisted as events)
            s = self.get(sid)
            if s:
                ev = {"seq": 0, "kind": "session", "session_id": sid,
                      "data": {k: s[k] for k in ("status", "title", "cost", "kind", "branch", "tokens_in", "tokens_out")}}
                for q in list(self._subs.get("*", ())):
                    q.put_nowait(ev)

    def add_usage(self, sid: str, cost: float, tin: int, tout: int) -> None:
        with self.lock:
            self.db.execute("UPDATE sessions SET cost=cost+?, tokens_in=tokens_in+?, tokens_out=tokens_out+?, "
                            "updated=? WHERE id=?", (cost, tin, tout, time.time(), sid))

    def list(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT id,kind,title,status,updated,cost,tokens_in,tokens_out,branch,project FROM sessions "
                               "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete(self, sid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM events WHERE session_id=?", (sid,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    # ------------------------------------------------------------- events
    def emit(self, sid: str, kind: str, data: dict | None = None) -> dict:
        ts = time.time()
        with self.lock:
            cur = self.db.execute("INSERT INTO events(session_id,ts,kind,data) VALUES (?,?,?,?)",
                                  (sid, ts, kind, json.dumps(data or {})))
            seq = cur.lastrowid
        ev = {"seq": seq, "ts": ts, "kind": kind, "data": data or {}}
        for q in list(self._subs.get(sid, ())) + list(self._subs.get("*", ())):
            q.put_nowait({**ev, "session_id": sid})
        return ev

    def publish(self, sid: str, kind: str, data: dict) -> None:
        """Live-only event (e.g. streamed text): sent to open windows, not saved."""
        ev = {"seq": 0, "kind": kind, "session_id": sid, "data": data}
        for q in list(self._subs.get(sid, ())) + list(self._subs.get("*", ())):
            q.put_nowait(ev)

    def find_event(self, sid: str, seq: int) -> dict | None:
        r = self.db.execute("SELECT seq,ts,kind,data FROM events WHERE session_id=? AND seq=?", (sid, seq)).fetchone()
        return {"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])} if r else None

    # ------------------------------------------------------------- totals
    def bump(self, **amounts: float) -> None:
        """Running totals for the savings view (spent, saved, answers by kind, ...)."""
        with self.lock:
            for k, v in amounts.items():
                if v:
                    self.db.execute("INSERT INTO totals(name,value) VALUES (?,?) "
                                    "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (k, float(v)))

    def totals(self) -> dict:
        return {r["name"]: r["value"] for r in self.db.execute("SELECT name,value FROM totals")}

    def events(self, sid: str, after: int = 0) -> list[dict]:
        rows = self.db.execute("SELECT seq,ts,kind,data FROM events WHERE session_id=? AND seq>? ORDER BY seq",
                               (sid, after)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]

    def subscribe(self, sid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(sid, set()).add(q)
        return q

    def unsubscribe(self, sid: str, q: asyncio.Queue) -> None:
        self._subs.get(sid, set()).discard(q)

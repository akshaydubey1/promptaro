# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Telemetry (learned routing scores) and response cache, both in SQLite.

Author: Akshay Dubey

The hot routing path never touches the database: aggregate stats are kept in
memory and written through on each recorded run.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PRIOR_WEIGHT = 6.0   # how many "virtual runs" the yaml prior is worth
FEEDBACK_WEIGHT = 3.0  # a person's thumbs up/down counts as this many automatic checks
DECAY_HALF_LIFE_S = 14 * 86400   # history halves in weight every two weeks: providers change,
                                # and a model exiled by old failures gets to earn its way back


def complexity_bucket(c: float) -> str:
    return "low" if c < 0.35 else ("mid" if c < 0.7 else "high")


def _decay(ts: float, now: float) -> float:
    return 0.5 ** (max(0.0, now - ts) / DECAY_HALF_LIFE_S)


@dataclass
class Agg:
    runs: float = 0.0           # decayed; first attempts only (a model answering a task that
    passes: float = 0.0         # already failed once faces a biased-harder question)
    cost_sum: float = 0.0       # all attempts — real money spent
    cost_n: float = 0.0
    latency_sum_ms: float = 0.0
    latency_n: float = 0.0
    out_tokens_sum: float = 0.0
    out_n: float = 0.0
    fb_runs: float = 0.0        # human feedback (thumbs, merged/discarded agent work)
    fb_good: float = 0.0


class Store:
    def __init__(self, path: str | Path = "data/router.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, request_id TEXT,
              model_id TEXT, task_type TEXT, bucket TEXT, passed INTEGER,
              cost_usd REAL, latency_ms REAL, tokens_in INTEGER, tokens_out INTEGER,
              attempt INTEGER, reasons TEXT);
            CREATE INDEX IF NOT EXISTS runs_key ON runs(model_id, task_type, bucket);
            CREATE TABLE IF NOT EXISTS feedback (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, model_id TEXT, task_type TEXT,
              bucket TEXT, good INTEGER, source TEXT);
            CREATE TABLE IF NOT EXISTS cache (
              key TEXT PRIMARY KEY, ts REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS spend (
              day TEXT PRIMARY KEY, usd REAL DEFAULT 0);
            """
        )
        self._agg: dict[tuple[str, str, str], Agg] = {}
        now = time.time()
        for ts, m, t, b, passed, cost, lat, out, att in self._db.execute(
            "SELECT ts, model_id, task_type, bucket, passed, cost_usd, latency_ms, tokens_out, attempt"
            " FROM runs"
        ):
            w = _decay(ts, now)
            a = self._agg.setdefault((m, t, b), Agg())
            a.cost_sum += w * (cost or 0.0)
            a.cost_n += w
            a.latency_sum_ms += w * (lat or 0.0)
            a.latency_n += w
            a.out_tokens_sum += w * (out or 0)
            a.out_n += w
            if (att or 1) == 1:
                a.runs += w
                a.passes += w * (passed or 0)
        for ts, m, t, b, good in self._db.execute("SELECT ts, model_id, task_type, bucket, good FROM feedback"):
            w = _decay(ts, now)
            a = self._agg.setdefault((m, t, b), Agg())
            a.fb_runs += w
            a.fb_good += w * (good or 0)

    # ---------------- telemetry ----------------
    def record(self, *, request_id: str, model_id: str, task_type: str, complexity: float, passed: bool,
               cost_usd: float, latency_ms: float, tokens_in: int, tokens_out: int, attempt: int,
               reasons: list[str]) -> None:
        b = complexity_bucket(complexity)
        with self._lock:
            self._db.execute(
                "INSERT INTO runs(ts,request_id,model_id,task_type,bucket,passed,cost_usd,latency_ms,"
                "tokens_in,tokens_out,attempt,reasons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), request_id, model_id, task_type, b, int(passed), cost_usd, latency_ms,
                 tokens_in, tokens_out, attempt, json.dumps(reasons)),
            )
            a = self._agg.setdefault((model_id, task_type, b), Agg())
            a.cost_sum += cost_usd
            a.cost_n += 1
            a.latency_sum_ms += latency_ms
            a.latency_n += 1
            a.out_tokens_sum += tokens_out
            a.out_n += 1
            if attempt == 1:
                a.runs += 1
                a.passes += int(passed)

    def feedback(self, *, model_id: str, task_type: str, complexity: float, good: bool,
                 source: str = "thumbs") -> None:
        """Learn from people: a thumbs-down lowers that model's expected quality for this kind of
        work, a thumbs-up (or merged agent work) raises it. Routing changes on the next request."""
        b = complexity_bucket(complexity)
        with self._lock:
            self._db.execute("INSERT INTO feedback(ts,model_id,task_type,bucket,good,source) VALUES (?,?,?,?,?,?)",
                             (time.time(), model_id, task_type, b, int(good), source))
            a = self._agg.setdefault((model_id, task_type, b), Agg())
            a.fb_runs += 1
            a.fb_good += int(good)

    def expected_quality(self, model_id: str, task_type: str, complexity: float, prior: float) -> float:
        """Beta-style blend of the configured prior with observed pass rate."""
        # The yaml prior is a mid-difficulty pass rate. Easy work fails less
        # often and hard work more, so scale the failure probability.
        adj = max(0.0, min(1.0, 1 - (1 - prior) * (0.3 + 1.2 * complexity)))
        a = self._agg.get((model_id, task_type, complexity_bucket(complexity)))
        if not a or (a.runs == 0 and a.fb_runs == 0):
            return adj
        w = FEEDBACK_WEIGHT
        return (adj * PRIOR_WEIGHT + a.passes + w * a.fb_good) / (PRIOR_WEIGHT + a.runs + w * a.fb_runs)

    def observed_latency_ms(self, model_id: str, task_type: str, complexity: float) -> float | None:
        a = self._agg.get((model_id, task_type, complexity_bucket(complexity)))
        return a.latency_sum_ms / a.latency_n if a and a.latency_n >= 3 else None

    def observed_out_tokens(self, model_id: str, task_type: str, complexity: float) -> float | None:
        a = self._agg.get((model_id, task_type, complexity_bucket(complexity)))
        return a.out_tokens_sum / a.out_n if a and a.out_n >= 3 else None

    # ---------------- daily spend (for the daily budget cap) ----------------
    def add_spend(self, usd: float) -> None:
        if usd <= 0:
            return
        with self._lock:
            self._db.execute("INSERT INTO spend(day,usd) VALUES (?,?) "
                             "ON CONFLICT(day) DO UPDATE SET usd=usd+excluded.usd",
                             (datetime.datetime.now(datetime.timezone.utc).date().isoformat(), usd))

    def spend_today(self) -> float:
        row = self._db.execute("SELECT usd FROM spend WHERE day=?",
                               (datetime.datetime.now(datetime.timezone.utc).date().isoformat(),)).fetchone()
        return float(row[0]) if row else 0.0

    def stats(self) -> list[dict]:
        out = []
        for (m, t, b), a in sorted(self._agg.items()):
            out.append({"model": m, "task_type": t, "complexity": b, "runs": round(a.runs, 1),
                        "pass_rate": round(a.passes / a.runs, 3) if a.runs else None,
                        "avg_cost_usd": round(a.cost_sum / a.cost_n, 6) if a.cost_n else None,
                        "avg_latency_ms": round(a.latency_sum_ms / a.latency_n) if a.latency_n else None,
                        "thumbs_up": round(a.fb_good, 1), "thumbs_down": round(a.fb_runs - a.fb_good, 1)})
        return out

    # ---------------- cache ----------------
    @staticmethod
    def cache_key(*parts: str) -> str:
        norm = "\x1f".join(" ".join(p.split()).lower() for p in parts)
        return hashlib.sha256(norm.encode()).hexdigest()

    def cache_get(self, key: str, ttl_s: float) -> dict | None:
        row = self._db.execute("SELECT ts, payload FROM cache WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[0] > ttl_s:
            return None
        return json.loads(row[1])

    def cache_drop_model(self, model_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM cache WHERE json_extract(payload, '$.model') = ?", (model_id,))

    def cache_put(self, key: str, payload: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache(key,ts,payload) VALUES (?,?,?)",
                             (key, time.time(), json.dumps(payload)))

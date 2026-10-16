# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Telemetry (learned routing scores) and response cache, both in SQLite.

Author: Akshay Dubey

The hot routing path never touches the database: aggregate stats are kept in
memory and written through on each recorded run.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PRIOR_WEIGHT = 6.0   # how many "virtual runs" the yaml prior is worth
FEEDBACK_WEIGHT = 3.0  # a person's thumbs up/down counts as this many automatic checks


def complexity_bucket(c: float) -> str:
    return "low" if c < 0.35 else ("mid" if c < 0.7 else "high")


@dataclass
class Agg:
    runs: int = 0
    passes: int = 0
    cost_sum: float = 0.0
    latency_sum_ms: float = 0.0
    out_tokens_sum: int = 0
    fb_runs: int = 0          # human feedback (thumbs, merged/discarded agent work)
    fb_good: int = 0


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
            """
        )
        self._agg: dict[tuple[str, str, str], Agg] = {}
        for row in self._db.execute(
            "SELECT model_id, task_type, bucket, COUNT(*), SUM(passed), SUM(cost_usd),"
            " SUM(latency_ms), SUM(tokens_out) FROM runs GROUP BY 1,2,3"
        ):
            self._agg[(row[0], row[1], row[2])] = Agg(row[3], row[4] or 0, row[5] or 0.0, row[6] or 0.0, row[7] or 0)
        for row in self._db.execute("SELECT model_id, task_type, bucket, COUNT(*), SUM(good) FROM feedback GROUP BY 1,2,3"):
            a = self._agg.setdefault((row[0], row[1], row[2]), Agg())
            a.fb_runs, a.fb_good = row[3], row[4] or 0

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
            a.runs += 1
            a.passes += int(passed)
            a.cost_sum += cost_usd
            a.latency_sum_ms += latency_ms
            a.out_tokens_sum += tokens_out

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
        return a.latency_sum_ms / a.runs if a and a.runs >= 3 else None

    def observed_out_tokens(self, model_id: str, task_type: str, complexity: float) -> float | None:
        a = self._agg.get((model_id, task_type, complexity_bucket(complexity)))
        return a.out_tokens_sum / a.runs if a and a.runs >= 3 else None

    def stats(self) -> list[dict]:
        out = []
        for (m, t, b), a in sorted(self._agg.items()):
            out.append({"model": m, "task_type": t, "complexity": b, "runs": a.runs,
                        "pass_rate": round(a.passes / a.runs, 3) if a.runs else None,
                        "avg_cost_usd": round(a.cost_sum / a.runs, 6) if a.runs else None,
                        "avg_latency_ms": round(a.latency_sum_ms / a.runs) if a.runs else None,
                        "thumbs_up": a.fb_good, "thumbs_down": a.fb_runs - a.fb_good})
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

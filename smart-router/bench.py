# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Benchmark the routing decision (classify + select + skills + context trim).

Author: Akshay Dubey
Usage: python bench.py [iterations]
"""
import os
import statistics
import sys
import tempfile
import time

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

import yaml  # noqa: E402

os.environ.setdefault("ANTHROPIC_API_KEY", "bench-only")   # makes models routable; no calls are made
from router.engine import Router, RunRequest  # noqa: E402
from tests.conftest import STATIC_MODELS  # noqa: E402


def bench_root() -> str:
    """Fixed model list so the benchmark needs no network."""
    root = Path(tempfile.mkdtemp())
    (root / "config").mkdir()
    shutil.copytree("skills", root / "skills")
    cfg = yaml.safe_load(Path("config/models.yaml").read_text())
    cfg["catalog"]["enabled"] = False
    cfg["models"] = STATIC_MODELS + cfg["models"]
    (root / "config" / "models.yaml").write_text(yaml.safe_dump(cfg))
    return str(root)

PROMPTS = [
    ("hello there", ""),
    ("write a python function that merges two sorted lists, with pytest tests", ""),
    ("Fix the bug in this terraform module for our GKE node pool", "resource \"google_container_node_pool\" {}\n" * 50),
    ("research current kubernetes autoscaling options and cite sources", ""),
    ("draft a short email to the team about Friday's release window", ""),
    ("Architect a production-grade, distributed, multi-region event pipeline with security trade-offs", ""),
    ("summarize the key decisions in this document", "Paragraph about deployments and services.\n\n" * 6000),
]


def main(n: int = 500) -> None:
    r = Router(bench_root(), db_path=os.path.join(tempfile.mkdtemp(), "bench.db"))
    for p, c in PROMPTS:                      # warm-up
        r.plan(RunRequest(p, c))
    rows = []
    for p, c in PROMPTS:
        ts = []
        for _ in range(n):
            t = time.perf_counter()
            _, d, *_ = r.plan(RunRequest(p, c))
            ts.append((time.perf_counter() - t) * 1000)
        ts.sort()
        rows.append((p[:58], d.primary.model.id, len(c), statistics.median(ts), ts[int(n * 0.99) - 1]))
    print(f"{'instruction':60} {'model':14} {'ctx chars':>9} {'p50 ms':>8} {'p99 ms':>8}")
    for p, m, cl, p50, p99 in rows:
        print(f"{p:60} {m:14} {cl:>9} {p50:>8.3f} {p99:>8.3f}")
    worst = max(r[4] for r in rows)
    print(f"\nWorst p99: {worst:.2f} ms  ->  target <100 ms: {'MET' if worst < 100 else 'MISSED'}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500)

# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Command line: `python -m router.cli "your instruction"`. Author: Akshay Dubey"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .engine import Router, RunRequest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Give one instruction; the router picks model, skills and checks.")
    ap.add_argument("instruction", nargs="?", help="what you want done (or pipe it on stdin)")
    ap.add_argument("-c", "--context-file", help="file to use as context")
    ap.add_argument("-t", "--tests-file", help="pytest file the result must pass")
    ap.add_argument("--prefer", choices=["balanced", "cheap", "fast", "best"], default="balanced")
    ap.add_argument("--max-cost", type=float, help="USD ceiling for this request")
    ap.add_argument("--model", help="force a model id from config/models.yaml")
    ap.add_argument("--review", choices=["off", "auto", "always"])
    ap.add_argument("--private", action="store_true",
                    help="don't send this to free providers that may train on prompts")
    ap.add_argument("--free", choices=["auto", "on", "off"], default="auto",
                    help="allow free models (default: the app's 'free models' setting)")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--route-only", action="store_true", help="show the routing decision, don't call a model")
    ap.add_argument("--gateway-token", action="store_true",
                    help="print the coding-tools gateway token and exit")
    ap.add_argument("--json", action="store_true", help="print the full trace as JSON")
    a = ap.parse_args()

    if a.gateway_token:
        from . import gateway
        print(gateway.gateway_token())
        return

    instruction = a.instruction or sys.stdin.read()
    if not instruction.strip():
        ap.error("no instruction given")
    ctx = open(a.context_file).read() if a.context_file else ""
    tests = open(a.tests_file).read() if a.tests_file else None

    async def go():
        r = Router(ROOT)
        try:
            return await r.run(RunRequest(instruction, ctx, tests=tests, prefer=a.prefer, max_cost_usd=a.max_cost,
                                          force_model=a.model, use_cache=not a.no_cache, cross_review=a.review,
                                          dry_run=a.route_only, private=a.private,
                                          allow_free={"auto": None, "on": True, "off": False}[a.free]))
        finally:
            await r.aclose()

    res = asyncio.run(go())
    d = res.as_dict()
    if a.json or a.route_only:
        print(json.dumps(d, indent=2, default=str))
        return
    print(res.output)
    t = d["totals"]
    status = "passed checks" if res.passed else "did NOT pass checks"
    print(f"\n--- {res.model} | {status} | {t['model_calls']} call(s) | ${t['cost_usd']:.4f} | "
          f"routing {t['routing_ms']:.2f} ms | total {t['total_ms']/1000:.1f}s"
          f"{' | cached' if res.cached else ''}", file=sys.stderr)


if __name__ == "__main__":
    main()

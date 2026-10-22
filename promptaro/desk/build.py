# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Fingerprint of the code on disk, so a running Promptaro can tell it has been updated.

Author: Akshay Dubey
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build() -> str:
    router = Path(os.environ.get("SMART_ROUTER_ROOT", HERE.parent.parent / "smart-router")).resolve() / "router"
    files = sorted(list(HERE.rglob("*.py")) + list((HERE / "ui").glob("*")) +
                   (list(router.glob("*.py")) if router.is_dir() else []))
    h = hashlib.sha1()
    for f in files:
        if f.is_file() and "__pycache__" not in f.parts:
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


if __name__ == "__main__":
    print(build())

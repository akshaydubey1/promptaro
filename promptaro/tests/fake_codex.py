#!/usr/bin/env python3
# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
"""Stand-in for OpenAI's `codex` CLI in tests (same output shapes as codex-cli 0.162 `exec --json`)."""
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
if a[:2] == ["login", "status"]:
    mode = os.environ.get("FAKE_CODEX_MODE", "chatgpt")
    if mode == "none":
        print("Not logged in", file=sys.stderr); sys.exit(1)
    print("Logged in using ChatGPT" if mode == "chatgpt" else "Logged in using an API key - sk-***", file=sys.stderr)
    sys.exit(0)
if a[:1] == ["exec"]:
    Path(os.environ["FAKE_CODEX_LOG"]).write_text(json.dumps(
        {"argv": a, "stdin": sys.stdin.buffer.read().decode("utf-8"),
         "has_api_key": "OPENAI_API_KEY" in os.environ}))
    out = lambda d: print(json.dumps(d), flush=True)
    out({"type": "thread.started", "thread_id": "thread-9"})
    out({"type": "turn.started"})
    if os.environ.get("FAKE_CODEX_FAIL"):
        out({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again later."}}); sys.exit(1)
    out({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Fixing add()."}})
    out({"type": "item.started", "item": {"id": "i1", "type": "file_change", "status": "in_progress",
                                          "changes": [{"path": "calc.py", "kind": "update"}]}})
    p = Path("calc.py"); p.write_text(p.read_text().replace("a - b", "a + b"))
    out({"type": "item.completed", "item": {"id": "i1", "type": "file_change", "status": "completed",
                                            "changes": [{"path": "calc.py", "kind": "update"}]}})
    out({"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": "pytest -q",
                                          "aggregated_output": "", "status": "in_progress"}})
    out({"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "pytest -q",
                                            "aggregated_output": "1 passed", "exit_code": 0, "status": "completed"}})
    out({"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Fixed add(); 1 test passes."}})
    out({"type": "turn.completed", "usage": {"input_tokens": 3000, "cached_input_tokens": 1000, "output_tokens": 400}})
    sys.exit(0)
sys.exit(2)

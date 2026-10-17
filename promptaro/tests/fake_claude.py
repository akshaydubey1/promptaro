#!/usr/bin/env python3
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Stand-in for the `claude` CLI used in tests (same output shapes as Claude Code 2.1)."""
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
if a[:2] == ["auth", "status"]:
    mode = os.environ.get("FAKE_CLAUDE_MODE", "sub")
    print(json.dumps({"loggedIn": mode != "none", "authMethod": "api_key" if mode == "api" else "oauth_token",
                      "apiProvider": "firstParty"}))
    sys.exit(0)
if "-p" in a:
    Path(os.environ["FAKE_CLAUDE_LOG"]).write_text(json.dumps({"argv": a, "has_api_key": "ANTHROPIC_API_KEY" in os.environ}))
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        print("Error: usage limit reached", file=sys.stderr); sys.exit(1)
    out = lambda d: print(json.dumps(d), flush=True)
    out({"type": "system", "subtype": "init", "session_id": "sess-123"})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll fix add()."},
         {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "calc.py", "old_string": "a - b", "new_string": "a + b"}}]}})
    p = Path("calc.py"); p.write_text(p.read_text().replace("a - b", "a + b"))
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "edited", "is_error": False}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": "Fixed add(); tests pass.",
         "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 800}, "session_id": "sess-123"})
    sys.exit(0)
sys.exit(2)

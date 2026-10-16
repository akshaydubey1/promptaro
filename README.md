# Promptaro

Author: Akshay Dubey

Every AI model, the right one for each task. This repository holds two parts that ship together:

| Folder | What it is |
|---|---|
| [`tillwright/`](tillwright/README.md) | The desktop app: chat with any model, run coding agents on your repos, use free models from Claude Code, Aider and other coding tools, and see what smart routing saved. |
| [`smart-router/`](smart-router/README.md) | The engine: picks the cheapest model expected to do each request well (in under a millisecond, no model call), tracks free-tier limits, escalates on failure, learns from results, and serves the local gateway for coding tools. |

## Run it

- **Mac:** double-click `tillwright/Install Tillwright.command` once (adds the app to Applications), or `tillwright/Start Tillwright.command`.
- **Windows:** `tillwright/Install Tillwright.bat` or `tillwright/Start Tillwright.bat`.
- **Linux:** `tillwright/packaging/install-linux.sh` or `tillwright/launch.sh`.

The two folders must stay side by side.

## Tests

```bash
cd smart-router && python -m pytest -q     # 90 tests
cd tillwright && python -m pytest -q       # 59 tests
```

---
Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.

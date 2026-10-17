# Promptaro

Author: Akshay Dubey

Every AI model, the right one for each task. This repository holds two parts that ship together:

| Folder | What it is |
|---|---|
| [`promptaro/`](promptaro/README.md) | The desktop app: chat with any model, run coding agents on your repos, use free models from Claude Code, Aider and other coding tools, and see what smart routing saved. (Previously released as Tillwright — existing installs migrate automatically.) |
| [`smart-router/`](smart-router/README.md) | The engine: picks the cheapest model expected to do each request well (in under a millisecond, no model call), tracks free-tier limits, escalates on failure, learns from results, and serves the local gateway for coding tools. |

## Run it

- **Mac:** double-click `promptaro/Install Promptaro.command` once (adds the app to Applications), or `promptaro/Start Promptaro.command`.
- **Windows:** `promptaro/Install Promptaro.bat` or `promptaro/Start Promptaro.bat`.
- **Linux:** `promptaro/packaging/install-linux.sh` or `promptaro/launch.sh`.

The two folders must stay side by side.

## Migrating from Tillwright

First run moves `~/.akshay-router` and `~/.tillwright` into `~/.promptaro` (keys, settings,
sessions, worktrees — existing `~/.promptaro` files are never overwritten), rewrites saved
worktree paths, and still reads credentials from the old `akshay-smart-router` keychain
entries. `tw-claude` / `tw-aider` / `tw-env` and the `tillwright-*` model aliases keep
working alongside the new `promptaro-*` names. Set `PROMPTARO_NO_MIGRATE=1` to opt out.

## Tests

```bash
cd smart-router && python -m pytest -q     # 93 tests
cd promptaro && python -m pytest -q        # 71 tests
```

---
Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.

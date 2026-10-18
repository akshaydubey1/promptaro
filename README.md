# Promptaro

**A desktop app by Akshay Dubey for AI-powered chats, coding, and agent tasks.**

Author: Akshay Dubey

Every AI model, the right one for each task. This repository holds two parts that ship together:

| Folder | What it is |
|---|---|
| [`promptaro/`](promptaro/README.md) | The desktop app: chat with any model, run coding agents on your repos, use free models from Claude Code, Aider and other coding tools, and see what smart routing saved. (Previously released as Tillwright — existing installs migrate automatically.) |
| [`smart-router/`](smart-router/README.md) | The engine: picks the cheapest model expected to do each request well (in under a millisecond, no model call), tracks free-tier limits, escalates on failure, learns from results, and serves the local gateway for coding tools. |

## Run it — easiest path

Download or clone this folder, then **double-click one file. Promptaro installs itself and opens in your browser.**

| | Double-click this | What happens |
|---|---|---|
| **Mac** | `promptaro/Install Promptaro.command` | Installs **Promptaro.app** into Applications *and opens it*. From then on it's a normal app: Spotlight, Launchpad, Dock. (Not Apple-signed — if macOS refuses, right-click the file → **Open**.) |
| **Windows** | `promptaro/Install Promptaro.bat` | Adds **Promptaro** to the Start menu and desktop *and starts it*. From then on, click the shortcut. |
| **Linux** | `promptaro/packaging/install-linux.sh` | Adds **Promptaro** to your app menu *and starts it*. |

Works on macOS 11+ (Intel and Apple Silicon), Windows 10/11 and Linux.

First start takes about a minute (it sets up its Python tools). Afterwards it opens instantly and keeps
running in the background; **Connections → Quit Promptaro** stops it. No code needed anywhere — no
terminal commands, no build steps.

Want it without installing? `promptaro/Start Promptaro.command` / `.bat` (or `promptaro/launch.sh`) runs it in place.

The two folders must stay side by side.

## Migrating from Tillwright

First run moves `~/.akshay-router` and `~/.tillwright` into `~/.promptaro` (keys, settings,
sessions, worktrees — existing `~/.promptaro` files are never overwritten), rewrites saved
worktree paths, and still reads credentials from the old `akshay-smart-router` keychain
entries. `tw-claude` / `tw-aider` / `tw-env` and the `tillwright-*` model aliases keep
working alongside the new `promptaro-*` names. Set `PROMPTARO_NO_MIGRATE=1` to opt out.

## Tests

```bash
cd smart-router && python -m pytest -q     # 101 tests
cd promptaro && python -m pytest -q        # 75 tests
```

---
Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.

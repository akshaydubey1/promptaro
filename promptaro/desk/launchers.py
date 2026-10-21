# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Command-line launchers that run coding tools through Promptaro's gateway. Author: Akshay Dubey

  promptaro-claude   Claude Code, on your free (or chosen) models
  promptaro-aider    Aider
  promptaro-env      prints the variables, for any other tool:  eval "$(promptaro-env)"

Each one makes sure Promptaro is running, asks it for the gateway token (only
programs on this computer can; a web page can't), and starts the tool. The
token is never written into the scripts.

Tillwright-era tw-claude / tw-aider / tw-env keep working: the installer also
writes tiny wrappers that forward to the new names, so existing scripts,
aliases and muscle memory aren't broken.
"""
from __future__ import annotations

import os
import platform
import stat
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # the promptaro folder

_POSIX_HEAD = """#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Promptaro launcher ({what}) — Author: Akshay Dubey. Created by Promptaro; safe to delete.
PORT="${{PROMPTARO_PORT:-${{TILLWRIGHT_PORT:-{port}}}}}"
URL="http://127.0.0.1:$PORT"
if ! curl -fsS "$URL/healthz" >/dev/null 2>&1; then
  echo "Starting Promptaro…" >&2
  PROMPTARO_NO_WINDOW=1 TILLWRIGHT_NO_WINDOW=1 "{src}/launch.sh" --background >/dev/null 2>&1 || true
  for i in $(seq 1 120); do curl -fsS "$URL/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
fi
RESP=$(curl -sS -X POST -H "x-promptaro: 1" "$URL/api/gateway/token" 2>/dev/null)
TOKEN=$(printf '%s' "$RESP" | sed -nE 's/.*"token":"([^"]+)".*/\\1/p')
if [ -z "$TOKEN" ]; then
  case "$RESP" in
    *terms_required*) echo "Open Promptaro and accept the Terms of Use first." >&2 ;;
    *) echo "Promptaro isn't running. Open it and try again." >&2 ;;
  esac
  exit 1
fi
"""

POSIX = {
    "promptaro-claude": ("Claude Code", """export ANTHROPIC_BASE_URL="$URL" ANTHROPIC_AUTH_TOKEN="$TOKEN"
unset ANTHROPIC_API_KEY
command -v claude >/dev/null || { echo "Claude Code isn't installed: https://docs.claude.com/en/docs/claude-code/setup" >&2; exit 1; }
exec claude "$@"
"""),
    "promptaro-aider": ("Aider", """command -v aider >/dev/null || { echo "Aider isn't installed: https://aider.chat/docs/install.html" >&2; exit 1; }
exec aider --openai-api-base "$URL/v1" --openai-api-key "$TOKEN" --model openai/promptaro-auto "$@"
"""),
    "promptaro-env": ("environment for any tool", """cat <<EOF
export ANTHROPIC_BASE_URL="$URL"
export ANTHROPIC_AUTH_TOKEN="$TOKEN"
export OPENAI_BASE_URL="$URL/v1"
export OPENAI_API_BASE="$URL/v1"
export OPENAI_API_KEY="$TOKEN"
EOF
"""),
}

_WIN_TOKEN = ('for /f "delims=" %%t in (\'powershell -NoProfile -Command "try {{ (Invoke-RestMethod -Method Post '
              '-Headers @{{\'x-promptaro\'=\'1\'}} http://127.0.0.1:{port}/api/gateway/token).token }} '
              'catch {{ if ($_.ErrorDetails.Message -match \'terms_required\') {{ \'TERMS_REQUIRED\' }} }}"\') '
              'do set PRO_TOKEN=%%t\r\n'
              'if "%PRO_TOKEN%"=="TERMS_REQUIRED" (echo Open Promptaro and accept the Terms of Use first. & exit /b 1)\r\n'
              'if "%PRO_TOKEN%"=="" (echo Promptaro isn\'t running. Open it and try again. & exit /b 1)\r\n')
WINDOWS = {
    "promptaro-claude.cmd": ("set ANTHROPIC_BASE_URL=http://127.0.0.1:{port}\r\nset ANTHROPIC_AUTH_TOKEN=%PRO_TOKEN%\r\n"
                            "set ANTHROPIC_API_KEY=\r\nclaude %*\r\n"),
    "promptaro-aider.cmd": ("aider --openai-api-base http://127.0.0.1:{port}/v1 --openai-api-key %PRO_TOKEN% "
                           "--model openai/promptaro-auto %*\r\n"),
    "promptaro-env.cmd": ("echo set ANTHROPIC_BASE_URL=http://127.0.0.1:{port}\r\n"
                          "echo set ANTHROPIC_AUTH_TOKEN=%PRO_TOKEN%\r\n"
                          "echo set OPENAI_BASE_URL=http://127.0.0.1:{port}/v1\r\n"
                          "echo set OPENAI_API_BASE=http://127.0.0.1:{port}/v1\r\n"
                          "echo set OPENAI_API_KEY=%PRO_TOKEN%\r\n"),
}

# Legacy names forward to the new ones (POSIX exec; Windows call).
LEGACY_POSIX = {"tw-claude": "promptaro-claude", "tw-aider": "promptaro-aider", "tw-env": "promptaro-env"}
LEGACY_WINDOWS = {"tw-claude.cmd": "promptaro-claude.cmd", "tw-aider.cmd": "promptaro-aider.cmd",
                  "tw-env.cmd": "promptaro-env.cmd"}


def bin_dir() -> Path:
    if platform.system() == "Windows":
        home = Path(os.environ.get("PROMPTARO_HOME") or os.environ.get("TILLWRIGHT_HOME")
                    or Path.home() / ".promptaro")
        return home / "bin"
    return Path.home() / ".local" / "bin"


def on_path(d: Path) -> bool:
    return str(d) in os.environ.get("PATH", "").split(os.pathsep)


def installed() -> bool:
    d = bin_dir()
    return (d / ("promptaro-claude.cmd" if platform.system() == "Windows" else "promptaro-claude")).exists()


def install(port: int = 8770) -> dict:
    d = bin_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = []
    if platform.system() == "Windows":
        for name, body in WINDOWS.items():
            p = d / name
            p.write_text("@echo off\r\nrem Promptaro launcher by Akshay Dubey. Created by Promptaro; safe to delete.\r\n"
                         + _WIN_TOKEN.format(port=port) + body.format(port=port), encoding="utf-8")
            written.append(str(p))
        for legacy, target in LEGACY_WINDOWS.items():
            p = d / legacy
            p.write_text(f"@echo off\r\ncall \"%~dp0{target}\" %*\r\n", encoding="utf-8")
            written.append(str(p))
    else:
        for name, (what, body) in POSIX.items():
            p = d / name
            p.write_text(_POSIX_HEAD.format(what=what, port=port, src=HERE) + body, encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written.append(str(p))
        for legacy, target in LEGACY_POSIX.items():
            p = d / legacy
            p.write_text(f'#!/bin/bash\n# Promptaro compatibility shim — forwards to {target}.\n'
                         f'exec "$(dirname "$0")/{target}" "$@"\n', encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written.append(str(p))
    return {"dir": str(d), "files": written, "on_path": on_path(d)}

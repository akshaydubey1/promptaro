# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Command-line launchers that run coding tools through Tillwright's gateway. Author: Akshay Dubey

  tw-claude   Claude Code, on your free (or chosen) models
  tw-aider    Aider
  tw-env      prints the variables, for any other tool:  eval "$(tw-env)"

Each one makes sure Tillwright is running, asks it for the gateway token (only
programs on this computer can; a web page can't), and starts the tool. The
token is never written into the scripts.
"""
from __future__ import annotations

import os
import platform
import stat
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # the tillwright folder

_POSIX_HEAD = """#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Tillwright launcher ({what}) — Author: Akshay Dubey. Created by Tillwright; safe to delete.
PORT="${{TILLWRIGHT_PORT:-{port}}}"
URL="http://127.0.0.1:$PORT"
if ! curl -fsS "$URL/healthz" >/dev/null 2>&1; then
  echo "Starting Tillwright…" >&2
  TILLWRIGHT_NO_WINDOW=1 "{src}/launch.sh" --background >/dev/null 2>&1 || true
  for i in $(seq 1 120); do curl -fsS "$URL/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
fi
TOKEN=$(curl -fsS -X POST -H "x-tillwright: 1" "$URL/api/gateway/token" | sed -E 's/.*"token":"([^"]+)".*/\\1/')
if [ -z "$TOKEN" ]; then echo "Tillwright isn't running. Open it and try again." >&2; exit 1; fi
"""

POSIX = {
    "tw-claude": ("Claude Code", """export ANTHROPIC_BASE_URL="$URL" ANTHROPIC_AUTH_TOKEN="$TOKEN"
unset ANTHROPIC_API_KEY
command -v claude >/dev/null || { echo "Claude Code isn't installed: https://docs.claude.com/en/docs/claude-code/setup" >&2; exit 1; }
exec claude "$@"
"""),
    "tw-aider": ("Aider", """command -v aider >/dev/null || { echo "Aider isn't installed: https://aider.chat/docs/install.html" >&2; exit 1; }
exec aider --openai-api-base "$URL/v1" --openai-api-key "$TOKEN" --model openai/tillwright-auto "$@"
"""),
    "tw-env": ("environment for any tool", """cat <<EOF
export ANTHROPIC_BASE_URL="$URL"
export ANTHROPIC_AUTH_TOKEN="$TOKEN"
export OPENAI_BASE_URL="$URL/v1"
export OPENAI_API_BASE="$URL/v1"
export OPENAI_API_KEY="$TOKEN"
EOF
"""),
}

_WIN_TOKEN = ('for /f "delims=" %%t in (\'powershell -NoProfile -Command "(Invoke-RestMethod -Method Post '
              '-Headers @{{\'x-tillwright\'=\'1\'}} http://127.0.0.1:{port}/api/gateway/token).token"\') do set TW_TOKEN=%%t\r\n'
              'if "%TW_TOKEN%"=="" (echo Tillwright isn\'t running. Open it and try again. & exit /b 1)\r\n')
WINDOWS = {
    "tw-claude.cmd": ("set ANTHROPIC_BASE_URL=http://127.0.0.1:{port}\r\nset ANTHROPIC_AUTH_TOKEN=%TW_TOKEN%\r\n"
                      "set ANTHROPIC_API_KEY=\r\nclaude %*\r\n"),
    "tw-aider.cmd": ("aider --openai-api-base http://127.0.0.1:{port}/v1 --openai-api-key %TW_TOKEN% "
                     "--model openai/tillwright-auto %*\r\n"),
}


def bin_dir() -> Path:
    if platform.system() == "Windows":
        return Path.home() / ".tillwright" / "bin"
    return Path.home() / ".local" / "bin"


def on_path(d: Path) -> bool:
    return str(d) in os.environ.get("PATH", "").split(os.pathsep)


def installed() -> bool:
    d = bin_dir()
    return (d / ("tw-claude.cmd" if platform.system() == "Windows" else "tw-claude")).exists()


def install(port: int = 8770) -> dict:
    d = bin_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = []
    if platform.system() == "Windows":
        for name, body in WINDOWS.items():
            p = d / name
            p.write_text("@echo off\r\nrem Tillwright launcher by Akshay Dubey. Created by Tillwright; safe to delete.\r\n"
                         + _WIN_TOKEN.format(port=port) + body.format(port=port))
            written.append(str(p))
    else:
        for name, (what, body) in POSIX.items():
            p = d / name
            p.write_text(_POSIX_HEAD.format(what=what, port=port, src=HERE) + body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            written.append(str(p))
    return {"dir": str(d), "files": written, "on_path": on_path(d)}

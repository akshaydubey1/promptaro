#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. Licensed under the PolyForm Small Business License 1.0.0; see LICENSE.
# Promptaro launcher for macOS and Linux — Author: Akshay Dubey
#   ./launch.sh               run in this terminal (close it to stop)
#   ./launch.sh --background  run quietly in the background (used by Promptaro.app and the Linux menu entry)
# First run installs what it needs (about a minute). Opens as its own window
# (Chrome / Edge / Brave / Chromium app mode) or in your default browser.
set -e
cd "$(dirname "$0")"
HERE="$(pwd)"
PORT="${PROMPTARO_PORT:-${TILLWRIGHT_PORT:-8770}}"
URL="http://localhost:$PORT"
BG=0; [ "$1" = "--background" ] && BG=1
DATA_DIR="${PROMPTARO_HOME:-${TILLWRIGHT_HOME:-$HOME/.promptaro}}"
LOG="${PROMPTARO_LOG:-${TILLWRIGHT_LOG:-$DATA_DIR/promptaro.log}}"
mkdir -p "$(dirname "$LOG")" "$DATA_DIR"
export SMART_ROUTER_ROOT="${SMART_ROUTER_ROOT:-$(cd ../smart-router 2>/dev/null && pwd)}"

say() {   # progress: Terminal text, or a desktop notification when running in the background
  if [ $BG = 1 ]; then
    if command -v osascript >/dev/null; then osascript -e "display notification \"$1\" with title \"Promptaro\"" >/dev/null 2>&1 || true
    elif command -v notify-send >/dev/null; then notify-send Promptaro "$1" || true; fi
  else echo "$1"; fi
}
fail() {
  if [ $BG = 1 ] && command -v osascript >/dev/null; then
    osascript -e "display alert \"Promptaro couldn't start\" message \"$1\n\nDetails: $LOG\"" >/dev/null 2>&1 || true
  else echo "Promptaro couldn't start: $1"; [ -t 0 ] && read -n1 -p "Press any key to close"; fi
  exit 1
}
[ -n "$SMART_ROUTER_ROOT" ] && [ -d "$SMART_ROUTER_ROOT/router" ] || fail "The smart-router folder must sit next to the promptaro folder."

open_app() {
  [ -n "${PROMPTARO_NO_WINDOW:-$TILLWRIGHT_NO_WINDOW}" ] && return          # started by a command-line launcher
  if [ "$(uname)" = "Darwin" ]; then
    for app in "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium"; do
      for dir in /Applications "$HOME/Applications"; do
        if [ -d "$dir/$app.app" ]; then open -na "$dir/$app.app" --args --app="$URL" --new-window; return; fi
      done
    done
    open "$URL"; return
  fi
  for b in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser; do
    if command -v "$b" >/dev/null; then nohup "$b" --app="$URL" --new-window >/dev/null 2>&1 & return; fi
  done
  (xdg-open "$URL" >/dev/null 2>&1 &) || echo "Open $URL"
}

# Already running? Load any update first, then just bring the window up.
if H=$(curl -fsS "$URL/healthz" 2>/dev/null); then
  if echo "$H" | grep -q '"update_ready":true'; then
    say "Loading the updated version…"
    curl -fsS -X POST -H "x-promptaro: 1" "$URL/api/restart" >/dev/null 2>&1 || true
    for i in $(seq 1 40); do sleep 0.5; curl -fsS "$URL/healthz" 2>/dev/null | grep -q '"update_ready":false' && break; done
  fi
  open_app; exit 0
fi

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "First run: installing Python tools (about a minute)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >>"$LOG" 2>&1 || fail "Couldn't download the Python installer. Check your internet connection."
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ ! -x .venv/bin/python ] || [ requirements.txt -nt .venv/.installed ]; then
  say "Setting up Promptaro (one time)…"
  { [ -x .venv/bin/python ] || uv venv --quiet --python 3.12 .venv
    uv pip install --quiet --python .venv/bin/python -r requirements.txt; } >>"$LOG" 2>&1 \
    || fail "Setup didn't finish."
  touch .venv/.installed
fi

( for i in $(seq 1 120); do curl -fsS "$URL/healthz" >/dev/null 2>&1 && { open_app; break; }; sleep 0.25; done ) &
OPENER=$!
if [ $BG = 1 ]; then
  nohup .venv/bin/python -m uvicorn desk.server:app --host 127.0.0.1 --port "$PORT" --log-level warning >>"$LOG" 2>&1 &
  echo $! > "$DATA_DIR/promptaro.pid"
  wait $OPENER || true                 # return once the window is open
  curl -fsS "$URL/healthz" >/dev/null 2>&1 || fail "It stopped while starting."
  exit 0
fi
echo "Promptaro is running at $URL  (close this window to stop it)"
exec .venv/bin/python -m uvicorn desk.server:app --host 127.0.0.1 --port "$PORT" --log-level warning

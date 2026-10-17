#!/bin/bash
# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
# Promptaro Smart Router launcher — Author: Akshay Dubey
# Double-click to start. First run installs everything it needs (about a minute);
# later runs start in a couple of seconds. Then your browser opens the app.
set -e
cd "$(dirname "$0")"
PORT="${ROUTER_PORT:-8765}"
URL="http://localhost:$PORT"
open_url() { (command -v open >/dev/null && open "$1") || (command -v xdg-open >/dev/null && xdg-open "$1") || echo "Open $1"; }

# Already running? Just open it.
if curl -fsS "$URL/healthz" >/dev/null 2>&1; then open_url "$URL"; exit 0; fi

# uv manages Python for us, so the Mac's own Python version doesn't matter.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "First run: installing the Python manager (uv)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
fi
if [ ! -x .venv/bin/python ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Setting up (one time)…"
  uv venv --quiet --python 3.12 .venv
  uv pip install --quiet --python .venv/bin/python -r requirements.txt
  touch .venv/.installed
fi

echo "Promptaro Smart Router is running at $URL  (close this window to stop it)"
( for i in $(seq 1 40); do curl -fsS "$URL/healthz" >/dev/null 2>&1 && { open_url "$URL"; break; }; sleep 0.25; done ) &
exec .venv/bin/python -m uvicorn router.api:app --host 127.0.0.1 --port "$PORT" --log-level warning

#!/usr/bin/env bash
# One command for a local demo: fresh demo data, the server, and a browser tab.
#   ./tools/demo.sh            (from dashboard/)
set -euo pipefail
cd "$(dirname "$0")/.."
python3 tools/make_demo_data.py data --now
PORT="${PORT:-8000}"
( sleep 1.5; open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null || true ) &
DATA_DIR=data exec python3 -m uvicorn app.main:app --port "$PORT"

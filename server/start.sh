#!/usr/bin/env bash
# Start BOTH server services (ingest :8600 + website :8501) in detached tmux sessions.
# Export your settings first, e.g.:
#   export SCOUT_ALLOWED_IPS=1.2.3.4 SCOUT_PARSE_WORKERS=8
#   ./server/start.sh
# Attach to watch logs: tmux attach -t ingest   (or -t web).  Detach: Ctrl-b then d.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v tmux >/dev/null || { echo "Install tmux first: apt install -y tmux"; exit 1; }

tmux kill-session -t ingest 2>/dev/null || true
tmux kill-session -t web 2>/dev/null || true
tmux new -d -s ingest "bash '$HERE/run_ingest.sh'"
tmux new -d -s web    "bash '$HERE/run_web.sh'"

echo "Started:"
echo "  ingest  → http://0.0.0.0:8600   (whoami: /whoami, health: /health)"
echo "  website → http://0.0.0.0:8501   (6-digit login)"
echo "Watch logs:  tmux attach -t ingest   |   tmux attach -t web"

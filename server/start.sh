#!/usr/bin/env bash
# Start BOTH server services (ingest :8600 + website :8501) in detached tmux sessions.
# Export your settings first, e.g.:
#   export SCOUT_ALLOWED_IPS=1.2.3.4 SCOUT_PARSE_WORKERS=8
#   ./server/start.sh
# Attach to watch logs: tmux attach -t ingest   (or -t web).  Detach: Ctrl-b then d.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v tmux >/dev/null || { echo "Install tmux first: apt install -y tmux"; exit 1; }

# Surface the fail-closed/open state to the operator's OWN terminal (the run script's
# message lands in the tmux pane they may never open).
if [ -z "${SCOUT_ALLOWED_IPS:-}" ] && [ -z "${SCOUT_INGEST_TOKEN:-}" ] && [ "${SCOUT_INGEST_OPEN:-}" != "1" ]; then
  echo "NOTE: SCOUT_ALLOWED_IPS is not set — the ingest API will REFUSE all requests."
  echo "      Start it anyway, open http://SERVER_IP:8600/whoami from your PC to learn your IP,"
  echo "      then:  export SCOUT_ALLOWED_IPS=<that ip>  &&  ./server/start.sh"
fi

# Pass env explicitly into each session (-e). A persistent tmux server would otherwise
# hand the new sessions its STALE environment, so a re-run with a freshly-exported
# SCOUT_ALLOWED_IPS would be ignored and the service would keep its old/open whitelist.
ENVS=(-e "SCOUT_HOME=${SCOUT_HOME:-$(cd "$HERE/.." && pwd)}"
      -e "SCOUT_PARSE_WORKERS=${SCOUT_PARSE_WORKERS:-8}"
      -e "SCOUT_ALLOWED_IPS=${SCOUT_ALLOWED_IPS:-}"
      -e "SCOUT_INGEST_TOKEN=${SCOUT_INGEST_TOKEN:-}"
      -e "SCOUT_TRUST_PROXY=${SCOUT_TRUST_PROXY:-}"
      -e "SCOUT_PROXY=${SCOUT_PROXY:-}"
      -e "SCOUT_INGEST_OPEN=${SCOUT_INGEST_OPEN:-}"
      -e "SCOUT_LOGIN=${SCOUT_LOGIN:-1}")

tmux kill-session -t ingest 2>/dev/null || true
tmux kill-session -t web 2>/dev/null || true
tmux new -d -s ingest "${ENVS[@]}" "bash '$HERE/run_ingest.sh'"
tmux new -d -s web    "${ENVS[@]}" "bash '$HERE/run_web.sh'"

echo "Started:"
echo "  ingest  → http://0.0.0.0:8600   (whoami: /whoami, health: /health)"
echo "  website → http://0.0.0.0:8501   (6-digit login)"
echo "Watch logs:  tmux attach -t ingest   |   tmux attach -t web"

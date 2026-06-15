#!/usr/bin/env bash
# CS2 Scout — server launch (Linux VPS). Public website with the 6-digit login gate.
#   Open http://SERVER_IP:8501  (also open the port: ufw allow 8501 + cloud firewall)
set -euo pipefail
cd "$(dirname "$0")"
export SCOUT_HOME="${SCOUT_HOME:-$(pwd)}"
export SCOUT_LOGIN=1                              # enable the 6-digit login gate
export SCOUT_PARSE_WORKERS="${SCOUT_PARSE_WORKERS:-8}"   # parse this many demos at once

exec .venv/bin/python -m streamlit run src/scout/ui/app.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true

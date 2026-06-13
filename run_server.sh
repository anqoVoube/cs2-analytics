#!/usr/bin/env bash
# CS2 Scout — server launch (Linux VPS). Binds to localhost; reach it via an SSH tunnel:
#   ssh -L 8501:localhost:8501 user@SERVER_IP   then open http://localhost:8501
set -euo pipefail
cd "$(dirname "$0")"
export SCOUT_HOME="${SCOUT_HOME:-$(pwd)}"
exec .venv/bin/python -m streamlit run src/scout/ui/app.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --server.headless true

#!/usr/bin/env bash
# Analytics website (server side) — view-only (no Auto-scout; scouting is on your PC).
# Public on :8501 with the 6-digit login gate.
set -euo pipefail
cd "$(dirname "$0")/.."                         # repo root (where .venv lives)
export SCOUT_HOME="${SCOUT_HOME:-$(pwd)}"
export SCOUT_LOGIN="${SCOUT_LOGIN:-1}"          # 6-digit login gate on
export SCOUT_ROLE=server                        # analytics pages only

exec .venv/bin/python -m streamlit run src/scout/ui/app.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true

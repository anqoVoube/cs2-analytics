#!/usr/bin/env bash
# CS2 Scout — ingest service (server side). Receives presigned demo URLs from your
# local app, downloads + parses them (fast, in-EU) into the shared cache that the
# website (run_server.sh) reads.
#   Open the port too: ufw allow 8600  + the cloud firewall.
set -euo pipefail
cd "$(dirname "$0")"
export SCOUT_HOME="${SCOUT_HOME:-$(pwd)}"
export SCOUT_PARSE_WORKERS="${SCOUT_PARSE_WORKERS:-8}"
# REQUIRED shared secret — put the SAME value in the local app's "Ingest token" field.
: "${SCOUT_INGEST_TOKEN:?set SCOUT_INGEST_TOKEN to a secret string, e.g. export SCOUT_INGEST_TOKEN=...}"

exec .venv/bin/python -m uvicorn scout.server.ingest_service:app \
  --host 0.0.0.0 --port 8600

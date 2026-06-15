#!/usr/bin/env bash
# Ingest API (server side) — receives signed demo URLs from your whitelisted PC,
# downloads + parses them into the shared cache. Port 8600.
#
# Whitelist your PC's IP:  export SCOUT_ALLOWED_IPS=1.2.3.4   (comma-separated for more)
# Don't know it yet? Start anyway, then open http://SERVER_IP:8600/whoami from your PC.
set -euo pipefail
cd "$(dirname "$0")/.."                         # repo root (where .venv lives)
export SCOUT_HOME="${SCOUT_HOME:-$(pwd)}"
# Leave SCOUT_PARSE_WORKERS unset to auto-size to RAM (avoids swap thrash). Override
# only if you know the box can handle more (each parse peaks ~2 GB).

if [ -z "${SCOUT_ALLOWED_IPS:-}" ] && [ -z "${SCOUT_INGEST_TOKEN:-}" ]; then
  echo "WARNING: no SCOUT_ALLOWED_IPS and no SCOUT_INGEST_TOKEN — the ingest API is OPEN."
  echo "         Open http://THIS_SERVER_IP:8600/whoami from your PC to find your IP,"
  echo "         then: export SCOUT_ALLOWED_IPS=<that ip>  and restart."
fi

# --no-proxy-headers: we deploy direct (no reverse proxy), so uvicorn must NOT rewrite
# the client IP from X-Forwarded-For — otherwise the IP whitelist is spoofable.
# (If you DO put a trusted proxy in front, set SCOUT_TRUST_PROXY=1 instead.)
PROXY_FLAG="--no-proxy-headers"
[ "${SCOUT_TRUST_PROXY:-}" = "1" ] && PROXY_FLAG="--proxy-headers"
exec .venv/bin/python -m uvicorn scout.server.ingest_service:app \
  --host 0.0.0.0 --port 8600 $PROXY_FLAG

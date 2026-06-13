#!/usr/bin/env bash
# CS2 Scout — server launch (Linux VPS), reachable from your browser at http://SERVER_IP:8501
#
# SECURITY: this binds to 0.0.0.0 (public). Set a password so strangers can't use your
# saved FACEIT API key:
#     export SCOUT_PASSWORD='something-only-you-know'
#     ./run_server.sh
# Also open the firewall: ufw allow 8501  (and the port in your cloud provider's firewall).
set -euo pipefail
cd "$(dirname "$0")"
export SCOUT_HOME="${SCOUT_HOME:-$(pwd)}"

if [ -z "${SCOUT_PASSWORD:-}" ]; then
  echo "WARNING: SCOUT_PASSWORD is not set — the site will be OPEN to anyone with the IP."
  echo "         Set one with:  export SCOUT_PASSWORD='your-password'   then re-run."
fi

exec .venv/bin/python -m streamlit run src/scout/ui/app.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true

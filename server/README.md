# Server side (EU box)

Runs two things, both reusing the `scout` library (so the Rust parser does the
parsing): an **ingest API** (`:8600`) that your PC sends signed demo URLs to, and the
**analytics website** (`:8501`, view-only, 6-digit login). Your PC does the FACEIT
login + URL signing and pushes here; this box downloads + parses + shows.

## One-time setup

```bash
apt update && apt install -y python3.12 python3.12-venv python3-pip git tmux
git clone https://github.com/anqoVoube/cs2-analytics.git /opt/scout
cd /opt/scout && python3.12 -m venv .venv && .venv/bin/pip install -e ".[server]"
ufw allow 8501 && ufw allow 8600        # also open both in your cloud firewall
```

## Find the IP to whitelist

Start once (it runs open with a warning), then from **your PC's browser** open
`http://SERVER_IP:8600/whoami` — it shows the IP the server sees you from.

## Run

```bash
export SCOUT_ALLOWED_IPS=<the ip from /whoami>     # comma-separated for several
export SCOUT_PARSE_WORKERS=8                        # = core count
./server/start.sh
```

`start.sh` launches both in tmux. Reattach with `tmux attach -t ingest` / `-t web`.
Updates: `cd /opt/scout && git pull && .venv/bin/pip install -e ".[server]"`, then re-run.

## On your PC

`run_local.bat` → log in to FACEIT → set **Server URL** `http://SERVER_IP:8600` →
paste a match link. View results at `http://SERVER_IP:8501`.

Notes: this box needs no Chrome and no FACEIT key. If your home IP changes, update
`SCOUT_ALLOWED_IPS` and restart (`/whoami` shows the current one).

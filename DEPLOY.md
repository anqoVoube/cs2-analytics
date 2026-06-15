# Fast setup: local sender + EU server

Two simple roles:

- **Your PC** (`run_local.bat`) — one page: paste a match link → it logs into FACEIT, signs
  the demo links, and sends them to your server. No 300 MB download on your slow link.
- **EU server** (`server/` folder) — whitelists your PC's IP, then **downloads + parses** the
  demos (fast, in-EU) and serves the **analytics website**. You view the battle plan there.

```
Your PC  (run_local.bat)                 EU server  (server/start.sh)
────────────────────────                 ─────────────────────────────
log in to FACEIT + sign links            ingest  :8600  ── download + parse
   └─ send signed URLs ─────────────────▶ website :8501  ◀── you view analytics
```

## Server (one-time)

Full details in **[server/README.md](server/README.md)**. Short version, on a CPU-optimized
EU box (Hetzner CCX33 = 8 vCPU/32 GB is ideal):

```bash
apt update && apt install -y python3.12 python3.12-venv python3-pip git tmux
git clone https://github.com/anqoVoube/cs2-analytics.git /opt/scout
cd /opt/scout && python3.12 -m venv .venv && .venv/bin/pip install -e ".[server]"
ufw allow 8501 && ufw allow 8600          # + open both in your cloud firewall
```

Find your IP to whitelist: start once (`./server/start.sh`), then open
`http://SERVER_IP:8600/whoami` from your PC. Then:

```bash
export SCOUT_ALLOWED_IPS=<that ip>   SCOUT_PARSE_WORKERS=8
./server/start.sh
```

That launches the ingest API (`:8600`) and the website (`:8501`, 6-digit login) in tmux.

## Your PC

Run **`run_local.bat`** → **⚙️ Settings**:
1. **Log in to FACEIT** (real Chrome, once).
2. **Server URL:** `http://SERVER_IP:8600` → **🔌 Test server** (green). Leave the token blank
   (auth is your whitelisted IP). Use **🪪 What's my IP?** if you need the value to whitelist.
3. Paste a match link → scout. Your PC signs the links and ships them; the server does the rest.

## View

Open **`http://SERVER_IP:8501`**, enter `floor(code ÷ 2)`, pick the team + map on **⚔️ Battle plan**.

## Notes

- **No server, all on one PC?** Just run `run_website.bat` (every page, no login, downloads locally).
- The login gate is a light personal speed-bump, not real security.
- If your home IP changes you'll get 403s — update `SCOUT_ALLOWED_IPS` (`/whoami` shows the new one) and restart.
- Parsing uses all cores (`SCOUT_PARSE_WORKERS`); demos load from the parsed cache, so you can delete
  `.dem` files on the server to save disk.

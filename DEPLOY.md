# Deploying CS2 Scout with a fast EU server

The fast setup splits the work:

- **Your PC (local app):** logs into FACEIT in a real browser and *signs* the demo links
  (the only step that needs your real login + IP). It then sends just the signed URLs to the
  server — no 300 MB download on your slow link.
- **EU server:** receives the signed URLs, **downloads + parses** the demos (fast, in-EU,
  parallel across cores), and serves the **analytics website** (with a 6-digit login gate).
- **You view** the battle plan on the server's website from anywhere.

```
Your PC (local)                         EU server (Amsterdam)
───────────────                         ─────────────────────
log in to FACEIT (browser)              ingest service  :8600  ── download + parse
sign links ───────────────────────────▶ (cache)
                                        website         :8501  ◀── you view analytics
```

## 1. Create the server

Pick a **CPU-optimized** VPS in the **EU (Amsterdam/Falkenstein/Helsinki)** so it's near the
demo CDN and parses fast:

- **Hetzner CCX33** (8 dedicated vCPU / 32 GB / NVMe, ~€30/mo) is the sweet spot. DigitalOcean
  "CPU-Optimized 8 vCPU" is equivalent. Avoid shared/burstable CPU — parsing pins a core.
- **OS: Ubuntu 24.04 LTS**. You get an IP + `root@IP` SSH login.

## 2. Install on the server

```bash
apt update && apt install -y python3.12 python3.12-venv python3-pip git
git clone https://github.com/anqoVoube/cs2-analytics.git /opt/scout
cd /opt/scout
python3.12 -m venv .venv
.venv/bin/pip install -e ".[server]"      # [server] adds fastapi + uvicorn
```

The server needs **no** Chrome/Playwright (no browser there) and no FACEIT key — it only
downloads signed URLs and parses. Future updates: `cd /opt/scout && git pull`.

## 3. Open the firewall

Open **8501** (website) and **8600** (ingest) — in `ufw` **and** your cloud provider's firewall:

```bash
ufw allow 8501 && ufw allow 8600
```

## 4. Run both services (in tmux so they survive logout)

Pick a secret token (any string) — you'll paste the same one into the local app.

```bash
apt install -y tmux
tmux new -s scout
# --- window 1: ingest service ---
cd /opt/scout
export SCOUT_INGEST_TOKEN='choose-a-long-secret'
export SCOUT_PARSE_WORKERS=8          # = your core count
./run_ingest.sh
# Ctrl-b c  → new window for the website:
cd /opt/scout
export SCOUT_PARSE_WORKERS=8
./run_server.sh
# detach: Ctrl-b d
```

- `run_server.sh` serves the **website** on `:8501` with the **6-digit login gate**
  (it shows a code N; enter `floor(N/2)` for 24h access).
- `run_ingest.sh` serves the **ingest API** on `:8600` (needs `SCOUT_INGEST_TOKEN`).

## 5. Point your local app at the server

On your PC, run the app (`run_website.bat`) → **🔎 Auto-scout → ⚙️ Settings**:

1. **Log in to FACEIT** (opens real Chrome — log in once).
2. **Server URL:** `http://YOUR_SERVER_IP:8600`  ·  **Ingest token:** the secret from step 4.
   Click **🔌 Test server** → should be green.
3. Paste a match link and scout. Your PC signs the links and ships them to the server; the
   server downloads + parses.

## 6. View the analytics

Open **`http://YOUR_SERVER_IP:8501`** in any browser, pass the login gate, and pick the
opponent team + map on the **⚔️ Battle plan** page.

## Notes

- The login gate is light (anyone who knows the "divide the code by 2" rule passes). Don't put
  anything sensitive behind it; it's a personal speed bump.
- Want the website private instead of public? Edit `run_server.sh` to `--server.address 127.0.0.1`
  and reach it via `ssh -L 8501:localhost:8501 root@IP`.
- Demos load from the parsed cache, so you can delete `.dem` files on the server to save disk;
  the analytics still work.

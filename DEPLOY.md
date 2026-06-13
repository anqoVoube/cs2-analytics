# Deploying CS2 Scout on a server (Amsterdam VPS)

Run the whole tool on a VPS inside Europe so demo downloads aren't blocked. You view
the web UI from your PC; the heavy 300 MB demos stay on the server, only the light web
UI travels to you.

## 1. Create the server

Any cheap VPS works (Hetzner, DigitalOcean, Vultr, Contabo…). Pick:

- **Location: Amsterdam / Netherlands** (or Frankfurt) — must be in the EU so it can reach
  `demos-europe-central.backblaze.faceit-cdn.net`.
- **OS: Ubuntu 24.04 LTS**
- **Size: 2 vCPU / 4 GB RAM / 40+ GB disk** is plenty (demos are ~300 MB each; disk is the
  main thing — bump it if you'll keep many).

You'll get an IP and an SSH login (e.g. `root@1.2.3.4`).

## 2. Install dependencies on the server

SSH in (`ssh root@YOUR_IP`) and run:

```bash
apt update && apt install -y python3.12 python3.12-venv python3-pip git
# copy the project up first (see step 3), then:
cd /opt/scout
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

## 3. Get the project onto the server

From your Windows PC (PowerShell), copy the code up (skip the big local data/ — the server
re-downloads what it needs):

```powershell
scp -r C:\Analytics\src C:\Analytics\pyproject.toml C:\Analytics\.streamlit root@YOUR_IP:/opt/scout/
```

(or push it to a private GitHub repo and `git clone` it on the server — either works.)

## 4. Run it (bound to localhost, reached over an SSH tunnel — secure, no password needed)

On the server:

```bash
cd /opt/scout
export SCOUT_HOME=/opt/scout            # optional; defaults to the project folder anyway
./run_server.sh                          # or the explicit command inside it
```

`run_server.sh` binds Streamlit to `127.0.0.1` so it is **not** exposed to the internet.

From your Windows PC, open a tunnel (leave this window open) and then browse locally:

```powershell
ssh -L 8501:localhost:8501 root@YOUR_IP
```

Now open **http://localhost:8501** in your browser. You're viewing the server's app over an
encrypted tunnel — nothing is public, no password to manage.

### Keep it running after you log out

```bash
# simplest: tmux
apt install -y tmux
tmux new -s scout
./run_server.sh
# detach with Ctrl-b then d ; reattach later with: tmux attach -t scout
```

## 5. Use it

In the browser (http://localhost:8501):

1. **⚙️ Settings** — paste your FACEIT API key and nickname (the server has its own copy under
   `/opt/scout/data/`). Leave the proxy field empty — the server itself is the "Amsterdam exit".
2. **🔌 Test connection** — should be green immediately (the server can reach the CDN).
3. Paste your match link → **Scout** → downloads run at full server speed → **⚔️ Battle plan**.

## Notes

- All paths are relative to `SCOUT_HOME` (default = the project folder), so nothing is
  hardcoded to Windows anymore — the same code runs on Windows and Linux.
- Radar images auto-download on first use; no manual setup.
- If you'd rather expose it publicly with a password instead of the SSH tunnel, tell me and
  I'll add a login gate — but the tunnel is simpler and safer.

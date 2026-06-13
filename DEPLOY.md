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

## 2. Install and set up on the server

SSH in (`ssh root@YOUR_IP`) and run:

```bash
apt update && apt install -y python3.12 python3.12-venv python3-pip git
git clone https://github.com/anqoVoube/cs2-analytics.git /opt/scout
cd /opt/scout
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

That's it — the repo has no secrets or demo data; the server downloads what it needs and you
enter your API key in the UI (step 5). To pull future updates: `cd /opt/scout && git pull`.

## 4. Run it (bound to localhost, reached over an SSH tunnel — secure, no password needed)

`run_server.sh` binds Streamlit to `0.0.0.0`, so you open it directly in your browser at
`http://YOUR_SERVER_IP:8501`. Because that is public, **set a password** so nobody else can
use your saved FACEIT key.

On the server:

```bash
# 1. open the firewall for the port
ufw allow 8501            # if ufw is active; also open 8501 in your cloud provider's firewall

# 2. set a password (anything only you know) and launch
cd /opt/scout
export SCOUT_PASSWORD='choose-a-strong-password'
bash run_server.sh
```

Then on your PC, open **http://YOUR_SERVER_IP:8501**, enter that password, and you're in.

### Keep it running after you log out

```bash
apt install -y tmux
tmux new -s scout
export SCOUT_PASSWORD='choose-a-strong-password'
cd /opt/scout && bash run_server.sh
# detach with Ctrl-b then d ; reattach later with: tmux attach -t scout
```

> Prefer no public exposure at all? Skip the firewall + password, edit `run_server.sh` to use
> `--server.address 127.0.0.1`, and reach it over an SSH tunnel instead:
> `ssh -L 8501:localhost:8501 root@YOUR_IP` then open `http://localhost:8501`.

## 5. Use it

In the browser (`http://YOUR_SERVER_IP:8501`):

1. **⚙️ Settings** — paste your FACEIT API key and nickname (the server has its own copy under
   `/opt/scout/data/`). Leave the proxy field empty — the server itself is the "Amsterdam exit".
2. **🔌 Test connection** — should be green immediately (the server can reach the CDN).
3. Paste your match link → **Scout** → downloads run at full server speed → **⚔️ Battle plan**.

## Notes

- All paths are relative to `SCOUT_HOME` (default = the project folder), so nothing is
  hardcoded to Windows anymore — the same code runs on Windows and Linux.
- Radar images auto-download on first use; no manual setup.
- The login gate is active only when `SCOUT_PASSWORD` is set, so local Windows use stays
  password-free while the public server requires one.

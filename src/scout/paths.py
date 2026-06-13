"""Central, portable paths for the whole project.

Default base is the repository root (the folder containing `src/`), so the project
runs unchanged wherever it's checked out — Windows `C:/Analytics` locally, or e.g.
`/opt/scout` on a Linux server. Override with the SCOUT_HOME environment variable.
"""
from __future__ import annotations

import os
from pathlib import Path

# this file is <root>/src/scout/paths.py → parents[2] is <root>
_REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_DIR = Path(os.environ.get("SCOUT_HOME") or _REPO_ROOT).resolve()
DATA_DIR = BASE_DIR / "data"
CACHE_ROOT = DATA_DIR / "cache"
DEMOS_DIR = DATA_DIR / "demos"
RADAR_DIR = DATA_DIR / "radars"
REPORTS_DIR = BASE_DIR / "reports"

# small settings files
KEY_PATH = DATA_DIR / "faceit_key.txt"
NICK_PATH = DATA_DIR / "faceit_me.txt"
PROXY_PATH = DATA_DIR / "faceit_proxy.txt"
INDEX_PATH = CACHE_ROOT / "demo_index.json"
SCOUT_DIR = DEMOS_DIR / "faceit_scout"

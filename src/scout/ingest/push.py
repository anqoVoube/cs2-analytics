"""Local → server push: send presigned demo URLs to the server's ingest service
so the server (fast, in-EU) does the download + parse instead of this machine.
"""
from __future__ import annotations

import requests

from ..paths import DATA_DIR

SERVER_PATH = DATA_DIR / "server_url.txt"
TOKEN_PATH = DATA_DIR / "server_token.txt"


def _read(path) -> str | None:
    try:
        v = path.read_text(encoding="utf-8").strip()
        return v or None
    except OSError:
        return None


def save_server(url: str, token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SERVER_PATH.write_text((url or "").strip().rstrip("/"), encoding="utf-8")
    TOKEN_PATH.write_text((token or "").strip(), encoding="utf-8")


def load_server() -> tuple[str | None, str | None]:
    return _read(SERVER_PATH), _read(TOKEN_PATH)


def server_health(url: str, timeout: int = 10) -> tuple[bool, str]:
    try:
        r = requests.get(f"{url.rstrip('/')}/health", timeout=timeout)
        if r.status_code == 200:
            return True, "Server reachable ✓"
        return False, f"Server responded {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def push_jobs(url: str, token: str | None, jobs: list[dict], proxy: str | None = None,
              timeout: int = 900) -> list[dict]:
    """POST [{match_id, url}] to the server; it downloads + parses. Returns per-job results."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.post(f"{url.rstrip('/')}/ingest",
                      json={"jobs": jobs, "proxy": proxy}, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json().get("results", [])

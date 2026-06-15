"""Local → server push: send presigned demo URLs to the server's ingest service
so the server (fast, in-EU) does the download + parse instead of this machine.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

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


def whoami(url: str, timeout: int = 10) -> tuple[bool, str]:
    """Ask the server which IP it sees us from — that's the value to whitelist."""
    try:
        r = requests.get(f"{url.rstrip('/')}/whoami", timeout=timeout)
        if r.status_code != 200:
            return False, f"/whoami returned {r.status_code}"
        j = r.json()
        ip = j.get("ip", "?")
        if j.get("whitelisted"):
            return True, f"Server sees you as {ip} — whitelisted ✓"
        return True, (f"Server sees you as {ip}. On the server run "
                      f"`export SCOUT_ALLOWED_IPS={ip}` and restart to whitelist it.")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def push_jobs_stream(url: str, token: str | None, jobs: list[dict],
                     timeout: int = 900) -> Iterator[dict]:
    """POST jobs and stream the server's NDJSON progress events as dicts.

    Yields {phase:"plan"|"download"|"parse"|"complete", ...}; the final "complete"
    event carries results:[{match_id, ok, cached, error}].
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with requests.post(f"{url.rstrip('/')}/ingest", json={"jobs": jobs}, headers=headers,
                       stream=True, timeout=timeout) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue

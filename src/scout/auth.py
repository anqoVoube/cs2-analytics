"""Lightweight login gate for the public server.

Active only when the env var SCOUT_LOGIN=1 (so local use stays open). The login
page shows a random 6-digit code N; entering floor(N/2) grants a token good for
24 hours. The token lives in the URL (?t=...) so a browser refresh stays logged in.

This is a light personal gate, not strong security — anyone who knows the
"divide by two" rule can pass. Don't put anything sensitive behind it.
"""
from __future__ import annotations

import json
import secrets
import time

from .paths import DATA_DIR

SESSIONS = DATA_DIR / "sessions.json"
TTL_SECONDS = 24 * 3600


def _load() -> dict:
    try:
        return json.loads(SESSIONS.read_text())
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    SESSIONS.parent.mkdir(parents=True, exist_ok=True)
    SESSIONS.write_text(json.dumps(d))


def new_code() -> int:
    """A random 6-digit challenge (100000-999999)."""
    return 100000 + secrets.randbelow(900000)


def expected_answer(code: int) -> int:
    return code // 2  # floor(code / 2)


def grant() -> str:
    """Issue a 24h token, pruning expired ones."""
    now = time.time()
    sessions = {k: v for k, v in _load().items() if v > now}
    token = secrets.token_urlsafe(16)
    sessions[token] = now + TTL_SECONDS
    _save(sessions)
    return token


def is_valid(token: str | None) -> bool:
    if not token:
        return False
    exp = _load().get(token)
    return bool(exp and exp > time.time())

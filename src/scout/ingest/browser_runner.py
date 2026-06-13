"""Launches the Playwright worker as a subprocess and streams its JSON events.

Kept separate from the worker so Streamlit never imports Playwright (its sync API
can't run inside Streamlit's ScriptRunner thread). Streamlit talks to the browser
only through this subprocess boundary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator

from scout.paths import BROWSER_PROFILE_DIR

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_logged_in() -> bool:
    """Cheap check: a persistent profile with cookies exists (best-effort)."""
    return BROWSER_PROFILE_DIR.exists() and any(BROWSER_PROFILE_DIR.iterdir())


def run(*worker_args: str) -> Iterator[dict]:
    """Run the worker and yield each emitted event as a dict.

    Hides only the worker's own console (CREATE_NO_WINDOW); the Chrome window it
    opens is still visible (needed for the login flow).
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "scout.ingest.browser_worker", *worker_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_CREATE_NO_WINDOW,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"phase": "log", "msg": line}
    finally:
        proc.wait()
        if proc.returncode:
            yield {"phase": "exit_error", "code": proc.returncode}

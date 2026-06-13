"""Playwright worker — runs as its OWN process (never inside Streamlit's thread).

Cloudflare blocks browsers that Playwright *launches* (they carry automation flags
like navigator.webdriver=true). So instead this launches a NORMAL Chrome itself
(no automation flags → looks human, navigator.webdriver is false) with a remote
debugging port + a dedicated persistent profile, then ATTACHES to it over CDP.
You log in once like a human (passing Cloudflare); the cleared session persists in
the profile and is reused for every later download. Subcommands:

    python -m scout.ingest.browser_worker login
    python -m scout.ingest.browser_worker download <match_id>=<resource_url> ...

Emits one JSON line per event to stdout. 'download' returns each presigned URL;
the parent reuses faceit.download_demo() to fetch the bytes.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from scout.ingest.faceit import DEMO_CDN, match_room_url
from scout.paths import BROWSER_PROFILE_DIR, BROWSER_SESSION_MARKER, SCOUT_DIR

DEBUG_PORT = int(os.environ.get("SCOUT_CDP_PORT", "9222"))
LOGIN_TIMEOUT_S = 600  # up to 10 min for the human to log in
PER_GOTO_TIMEOUT = 45_000
_PROBE_RESOURCE = f"https://{DEMO_CDN}/cs2/session-probe-1-1.dem.zst"  # dummy, only tests auth

_CHROME_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]

# Calls the download-url endpoint from inside the logged-in page: cookies +
# cf_clearance + Origin come from the real browser, and we attach the SPA's Bearer
# token (read from localStorage) since it's added per-request by an interceptor.
_IN_PAGE_FETCH = r"""async (resource_url) => {
  const find = (o) => {
    if (typeof o === "string")
      return (o.startsWith("http") && /(X-Amz|backblazeb2\.com|faceit-cdn|Authorization=)/.test(o)) ? o : null;
    if (o && typeof o === "object") for (const v of Object.values(o)) { const f = find(v); if (f) return f; }
    return null;
  };
  let bearer = null;
  for (const k of Object.keys(localStorage)) {
    const v = localStorage.getItem(k);
    if (!v) continue;
    if (/^ey[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\./.test(v)) { bearer = v; break; }
    try {
      const o = JSON.parse(v);
      const t = o.access_token || o.accessToken || o.token || (o.auth && (o.auth.access_token || o.auth.token));
      if (t && /^ey/.test(String(t))) { bearer = String(t); break; }
    } catch (e) {}
  }
  const headers = { "content-type": "application/json" };
  if (bearer) headers["authorization"] = "Bearer " + bearer.replace(/^"|"$/g, "");
  let r;
  try {
    r = await fetch("https://www.faceit.com/api/download/v2/demos/download-url",
      { method: "POST", credentials: "include", headers, body: JSON.stringify({ resource_url }) });
  } catch (e) { return { ok: false, status: 0, text: String(e) }; }
  const body = await r.text();
  if (!r.ok) return { ok: false, status: r.status, text: body.slice(0, 300) };
  let data; try { data = JSON.parse(body); } catch (e) { return { ok: false, status: 200, text: body.slice(0, 300) }; }
  const url = (data.payload && data.payload.download_url) || find(data);
  return url ? { ok: true, url } : { ok: false, status: 200, text: JSON.stringify(data).slice(0, 300) };
}"""


def emit(**info) -> None:
    sys.stdout.write(json.dumps(info) + "\n")
    sys.stdout.flush()


def _chrome_exe() -> str | None:
    for c in _CHROME_CANDIDATES:
        try:
            if c and c.exists():
                return str(c)
        except OSError:
            continue
    return None


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", DEBUG_PORT)) == 0


def _launch_chrome() -> None:
    exe = _chrome_exe()
    if not exe:
        raise RuntimeError("Google Chrome not found — install Chrome to use auto-download.")
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={BROWSER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
    ]
    args.append("--headless=new" if os.environ.get("SCOUT_BROWSER_HEADLESS") == "1"
                else "--start-maximized")
    args.append("https://www.faceit.com/en")
    # DETACHED so Chrome outlives this short-lived worker (reused by later runs)
    flags = (0x00000008 | 0x00000200) if sys.platform == "win32" else 0
    subprocess.Popen(args, creationflags=flags, close_fds=True)


def _ensure_chrome() -> None:
    if _port_open():
        return
    _launch_chrome()
    for _ in range(60):
        if _port_open():
            return
        time.sleep(0.5)
    raise RuntimeError("Chrome did not open its debug port.")


def _page(browser):
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    for pg in ctx.pages:
        if "faceit.com" in (pg.url or ""):
            return pg
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def _session_state(page) -> str:
    """Probe download-url in-page: 'ok' (authed), 'anon' (not logged in), 'challenge'."""
    try:
        res = page.evaluate(_IN_PAGE_FETCH, _PROBE_RESOURCE)
    except Exception:
        return "challenge"
    if res.get("ok"):
        return "ok"
    status = res.get("status")
    text = str(res.get("text", ""))
    if status == 403 and ("Just a moment" in text or "challenge" in text.lower()):
        return "challenge"
    if status in (0, None):
        return "challenge"
    if status == 401:
        return "anon"
    return "ok"  # 400/403/404/200… = past Cloudflare and session recognized


def cmd_login() -> int:
    emit(phase="launching", msg="Opening a real Chrome window — log in to FACEIT in it…")
    try:
        _ensure_chrome()
    except Exception as e:
        emit(phase="error", msg=str(e))
        return 2
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        try:
            page = _page(browser)
            try:
                page.goto("https://www.faceit.com/en", wait_until="domcontentloaded",
                          timeout=PER_GOTO_TIMEOUT)
            except Exception:
                pass
            emit(phase="awaiting_login", msg="Log in to FACEIT in the Chrome window…")
            for _ in range(LOGIN_TIMEOUT_S):
                if _session_state(page) == "ok":
                    BROWSER_SESSION_MARKER.write_text("ok", encoding="utf-8")
                    emit(phase="logged_in",
                         msg="Login confirmed — session saved. Leave that Chrome window open.")
                    return 0
                page.wait_for_timeout(1000)
            emit(phase="error", msg="Timed out waiting for login. Try again.")
            return 2
        finally:
            browser.close()  # detach only; the launched Chrome keeps running


def cmd_download(items: list[str]) -> int:
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _ensure_chrome()
    except Exception as e:
        emit(phase="needs_login", msg=str(e))
        return 3
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        try:
            page = _page(browser)
            try:
                page.goto("https://www.faceit.com/en", wait_until="domcontentloaded",
                          timeout=PER_GOTO_TIMEOUT)
            except Exception:
                pass
            state = _session_state(page)
            if state != "ok":
                BROWSER_SESSION_MARKER.unlink(missing_ok=True)
                emit(phase="needs_login",
                     msg=("Cloudflare challenge — re-run Log in to FACEIT." if state == "challenge"
                          else "Session expired / not logged in — run Log in to FACEIT."))
                return 3
            n = len(items)
            for i, it in enumerate(items, 1):
                mid, res = it.split("=", 1) if "=" in it else (it, it)
                emit(phase="start", match_id=mid, index=i, count=n)
                try:
                    page.goto(match_room_url(mid), wait_until="domcontentloaded",
                              timeout=PER_GOTO_TIMEOUT)
                except Exception:
                    pass
                try:
                    result = page.evaluate(_IN_PAGE_FETCH, res)
                except Exception as e:
                    result = {"ok": False, "status": 0, "text": str(e)[:300]}
                if result.get("ok"):
                    emit(phase="presigned", match_id=mid, index=i, count=n, url=result["url"])
                else:
                    status = result.get("status")
                    challenged = status in (401, 403) or "Just a moment" in str(result.get("text", ""))
                    emit(phase="error", match_id=mid, index=i, count=n, status=status,
                         msg=str(result.get("text", ""))[:200], challenged=bool(challenged))
            emit(phase="finished", count=n)
            return 0
        finally:
            browser.close()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("login")
    d = sub.add_parser("download")
    d.add_argument("items", nargs="+")
    args = ap.parse_args()
    try:
        return cmd_login() if args.mode == "login" else cmd_download(args.items)
    except Exception as e:
        emit(phase="fatal", msg=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

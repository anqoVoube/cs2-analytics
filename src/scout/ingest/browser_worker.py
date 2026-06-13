"""Playwright worker — runs as its OWN process (never inside Streamlit's thread).

Drives a persistent, logged-in real-Chrome profile so it sails through FACEIT's
Cloudflare challenge and auth the way a human browser does. Two subcommands:

    python -m scout.ingest.browser_worker login
    python -m scout.ingest.browser_worker download <match_id>=<resource_url> ...

Emits one JSON line per event to stdout (UTF-8, line-buffered) so the parent can
stream progress. The 'download' command does NOT fetch the demo bytes itself — it
just returns each presigned URL; the parent reuses faceit.download_demo() so all
the streaming/decompress/retry logic stays in one place.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from scout.ingest.faceit import DEMO_CDN, match_room_url
from scout.paths import BROWSER_PROFILE_DIR, BROWSER_SESSION_MARKER, SCOUT_DIR

LOGIN_TIMEOUT_S = 600  # up to 10 min for the human to log in
PER_DEMO_GOTO_TIMEOUT = 45_000
_PROBE_RESOURCE = f"https://{DEMO_CDN}/cs2/session-probe-1-1.dem.zst"  # dummy, just tests auth

# Reads the FACEIT access token (JWT) out of localStorage, then calls the
# download-url endpoint from inside the logged-in page — cookies + cf_clearance +
# correct Origin are all supplied by the browser automatically.
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


def _context(p):
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    common = dict(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=os.environ.get("SCOUT_BROWSER_HEADLESS") == "1",  # headed for real use
        viewport={"width": 1280, "height": 860},
        accept_downloads=True,
    )
    # real Chrome looks least like automation to Cloudflare and needs no download
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **common)
    except Exception:
        return p.chromium.launch_persistent_context(**common)


def _session_state(page) -> str:
    """Probe the download-url endpoint from the page. Returns 'ok' (authed + past
    Cloudflare), 'anon' (past Cloudflare but not logged in), or 'challenge'."""
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
    # any other recognized response (400/403/404/422/200…) means we're past
    # Cloudflare AND the session is recognized → good enough to sign real demos
    return "ok"


def cmd_login() -> int:
    from playwright.sync_api import sync_playwright

    emit(phase="launching", msg="Opening Chrome — log in to FACEIT in the window…")
    with sync_playwright() as p:
        ctx = _context(p)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.faceit.com/en", wait_until="domcontentloaded")
            emit(phase="awaiting_login", msg="Waiting for you to log in to FACEIT…")
            for _ in range(LOGIN_TIMEOUT_S):
                if _session_state(page) == "ok":
                    BROWSER_SESSION_MARKER.write_text("ok", encoding="utf-8")
                    emit(phase="logged_in",
                         msg="Login confirmed — session saved. You can close the window.")
                    return 0
                page.wait_for_timeout(1000)
            emit(phase="error", msg="Timed out waiting for login. Try again.")
            return 2
        finally:
            ctx.close()  # flush profile to disk


def cmd_download(items: list[str]) -> int:
    from playwright.sync_api import sync_playwright

    SCOUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = _context(p)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # establish the faceit.com origin, then confirm the session still works
            try:
                page.goto("https://www.faceit.com/en", wait_until="domcontentloaded",
                          timeout=PER_DEMO_GOTO_TIMEOUT)
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
                              timeout=PER_DEMO_GOTO_TIMEOUT)
                except Exception:
                    pass  # we only need the origin/session; room render isn't required
                try:
                    result = page.evaluate(_IN_PAGE_FETCH, res)
                except Exception as e:
                    result = {"ok": False, "status": 0, "text": str(e)[:300]}
                if result.get("ok"):
                    emit(phase="presigned", match_id=mid, index=i, count=n, url=result["url"])
                else:
                    status = result.get("status")
                    challenged = status in (401, 403) or "Just a moment" in str(result.get("text", ""))
                    emit(phase="error", match_id=mid, index=i, count=n,
                         status=status, msg=str(result.get("text", ""))[:200],
                         challenged=bool(challenged))
            emit(phase="finished", count=n)
            return 0
        finally:
            ctx.close()


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
    except Exception as e:  # never crash silently — the parent reads stdout
        emit(phase="fatal", msg=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Server-side ingest API: receives presigned demo URLs from the local minter,
downloads them (fast, from inside the EU) and parses them into the shared cache.

The Streamlit website on the same box then serves the analytics from that cache.

Run:  uvicorn scout.server.ingest_service:app --host 0.0.0.0 --port 8600
Auth: whitelist your PC's IP in SCOUT_ALLOWED_IPS (comma-separated). Optionally also
require a token via SCOUT_INGEST_TOKEN. The service FAILS CLOSED — if neither is set
it refuses every request (set SCOUT_INGEST_OPEN=1 only to deliberately run open).
"""
from __future__ import annotations

import ipaddress
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from ..analytics.loader import parse_all
from ..ingest.faceit import _parsed_match_ids, download_demo
from ..paths import SCOUT_DIR

app = FastAPI(title="CS2 Scout ingest")

TOKEN = os.environ.get("SCOUT_INGEST_TOKEN")
TRUST_PROXY = os.environ.get("SCOUT_TRUST_PROXY") == "1"   # only if behind a real proxy
OPEN_OK = os.environ.get("SCOUT_INGEST_OPEN") == "1"        # deliberate open mode
SERVER_PROXY = os.environ.get("SCOUT_PROXY") or None        # server's OWN egress proxy, if any

# demos only ever come from these hosts; anything else is rejected (anti-SSRF)
_ALLOWED_HOST_SUFFIXES = (".backblazeb2.com", ".faceit-cdn.net")
_MATCH_ID_RE = re.compile(r"^1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _norm_ip(s: str) -> str:
    """Canonicalize an IP (collapses IPv4-mapped IPv6 ::ffff:1.2.3.4 → 1.2.3.4)."""
    try:
        ip = ipaddress.ip_address(s)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ip)
    except ValueError:
        return s


ALLOWED_IPS = {_norm_ip(ip.strip()) for ip in os.environ.get("SCOUT_ALLOWED_IPS", "").split(",")
               if ip.strip()}


class Job(BaseModel):
    match_id: str
    url: str


class IngestRequest(BaseModel):
    jobs: list[Job]
    proxy: str | None = None  # accepted for compat but IGNORED — server uses its own egress


def _client_ip(request: Request) -> str:
    # Do NOT trust X-Forwarded-For by default: with a direct (no-proxy) deployment any
    # client could spoof it and defeat the whitelist. Only honor it behind a real proxy.
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[-1].strip()  # rightmost = the trusted proxy's view
    return request.client.host if request.client else ""


def _check_auth(request: Request, authorization: str | None) -> None:
    if not ALLOWED_IPS and not TOKEN and not OPEN_OK:
        raise HTTPException(status_code=503,
                            detail="ingest not configured (set SCOUT_ALLOWED_IPS or SCOUT_INGEST_TOKEN)")
    if ALLOWED_IPS and _norm_ip(_client_ip(request)) not in ALLOWED_IPS:
        raise HTTPException(status_code=403, detail="IP not whitelisted")
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


def _valid_demo_url(u: str) -> bool:
    try:
        p = urlparse(u)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "ip_whitelist": bool(ALLOWED_IPS), "token_required": bool(TOKEN),
            "open": OPEN_OK and not ALLOWED_IPS and not TOKEN}


@app.get("/whoami")
def whoami(request: Request) -> dict:
    """The IP the server sees you from (the real socket peer) — whitelist this. Open so
    you can look it up before you're whitelisted."""
    ip = request.client.host if request.client else ""
    return {"ip": ip, "whitelisted": _norm_ip(ip) in ALLOWED_IPS}


@app.post("/ingest")
def ingest(req: IngestRequest, request: Request,
           authorization: str | None = Header(default=None)) -> dict:
    """Validate, download (parallel) any demos we don't have, then parse (parallel).

    Returns one result per job: {match_id, ok, cached, error}. Synchronous — the
    client should use a generous timeout (downloads + parse take a couple minutes).
    """
    _check_auth(request, authorization)
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)
    have = _parsed_match_ids()
    results: dict[str, dict] = {}
    to_download = []
    for job in req.jobs:
        if not _MATCH_ID_RE.match(job.match_id):  # prevents path traversal via filename
            results[job.match_id] = {"match_id": job.match_id, "ok": False, "cached": False,
                                     "error": "invalid match_id"}
            continue
        if not _valid_demo_url(job.url):  # anti-SSRF: only the demo CDN hosts, https only
            results[job.match_id] = {"match_id": job.match_id, "ok": False, "cached": False,
                                     "error": "url host not allowed"}
            continue
        dest = SCOUT_DIR / f"{job.match_id}.dem"
        if dest.exists() or job.match_id in have:
            results[job.match_id] = {"match_id": job.match_id, "ok": True, "cached": True,
                                     "error": None}
        else:
            to_download.append(job)

    def _dl(job: Job):  # server uses its OWN proxy (or none), never the client's
        dest = SCOUT_DIR / f"{job.match_id}.dem"
        try:
            download_demo(job.url, dest, proxy=SERVER_PROXY)
            return job.match_id, dest, None
        except Exception as e:  # noqa: BLE001
            return job.match_id, dest, f"download: {type(e).__name__}: {e}"

    downloaded = []
    if to_download:
        with ThreadPoolExecutor(max_workers=min(4, len(to_download))) as ex:
            for mid, dest, err in ex.map(_dl, to_download):
                if err:
                    results[mid] = {"match_id": mid, "ok": False, "cached": False, "error": err}
                else:
                    downloaded.append((mid, dest))

    if downloaded:
        parse_all(SCOUT_DIR)  # parallel across cores (process pool)
        now_parsed = _parsed_match_ids()
        for mid, _dest in downloaded:
            ok = mid in now_parsed
            results[mid] = {"match_id": mid, "ok": ok, "cached": False,
                            "error": None if ok else "parse failed"}

    ordered = [results.get(j.match_id, {"match_id": j.match_id, "ok": False, "cached": False,
                                        "error": "no result"}) for j in req.jobs]
    return {"results": ordered}

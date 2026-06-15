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
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
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
           authorization: str | None = Header(default=None)) -> StreamingResponse:
    """Validate → download (parallel) → parse (parallel), STREAMING progress as NDJSON.

    Lines: {phase:"download",done,total,count} during the download (bytes), then
    {phase:"parse",i,n} during parsing, then a final {phase:"complete",results:[...]}
    with one {match_id,ok,cached,error} per job. Auth is checked before streaming.
    """
    _check_auth(request, authorization)
    return StreamingResponse(_ingest_stream(req), media_type="application/x-ndjson")


def _ingest_stream(req: IngestRequest):
    def line(d: dict) -> str:
        return json.dumps(d) + "\n"

    SCOUT_DIR.mkdir(parents=True, exist_ok=True)
    have = _parsed_match_ids()
    results: dict[str, dict] = {}
    to_download = []
    for job in req.jobs:
        if not _MATCH_ID_RE.match(job.match_id):  # prevents path traversal via filename
            results[job.match_id] = {"match_id": job.match_id, "ok": False, "cached": False,
                                     "error": "invalid match_id"}
        elif not _valid_demo_url(job.url):  # anti-SSRF: only the demo CDN hosts, https only
            results[job.match_id] = {"match_id": job.match_id, "ok": False, "cached": False,
                                     "error": "url host not allowed"}
        elif (SCOUT_DIR / f"{job.match_id}.dem").exists() or job.match_id in have:
            results[job.match_id] = {"match_id": job.match_id, "ok": True, "cached": True,
                                     "error": None}
        else:
            to_download.append(job)
    yield line({"phase": "plan", "to_download": len(to_download),
                "cached": sum(1 for r in results.values() if r.get("cached"))})

    downloaded = []
    if to_download:
        state = {j.match_id: {"done": 0, "total": 0} for j in to_download}
        lock = threading.Lock()

        def _dl(job: Job):  # server uses its OWN proxy (or none), never the client's
            def cb(done: int, total: int) -> None:
                with lock:
                    state[job.match_id]["done"] = done
                    state[job.match_id]["total"] = total
            download_demo(job.url, SCOUT_DIR / f"{job.match_id}.dem",
                          on_bytes=cb, proxy=SERVER_PROXY)

        ex = ThreadPoolExecutor(max_workers=min(4, len(to_download)))
        futs = {ex.submit(_dl, j): j for j in to_download}
        while not all(f.done() for f in futs):
            with lock:
                td = sum(s["done"] for s in state.values())
                tt = sum(s["total"] for s in state.values())
            yield line({"phase": "download", "done": td, "total": tt, "count": len(to_download)})
            time.sleep(0.5)
        for f, j in futs.items():
            try:
                f.result()
                downloaded.append(j.match_id)
            except Exception as e:  # noqa: BLE001
                results[j.match_id] = {"match_id": j.match_id, "ok": False, "cached": False,
                                       "error": f"download: {type(e).__name__}: {e}"}
        ex.shutdown(wait=True)

    if downloaded:
        prog = {"i": 0, "n": 0}

        def _do_parse() -> None:
            parse_all(SCOUT_DIR, progress=lambda i, n, name: prog.update(i=i, n=n))

        t = threading.Thread(target=_do_parse)
        t.start()
        while t.is_alive():
            yield line({"phase": "parse", "i": prog["i"], "n": prog["n"]})
            time.sleep(0.5)
        t.join()
        now_parsed = _parsed_match_ids()
        for mid in downloaded:
            ok = mid in now_parsed
            results[mid] = {"match_id": mid, "ok": ok, "cached": False,
                            "error": None if ok else "parse failed"}

    ordered = [results.get(j.match_id, {"match_id": j.match_id, "ok": False, "cached": False,
                                        "error": "no result"}) for j in req.jobs]
    yield line({"phase": "complete", "results": ordered})

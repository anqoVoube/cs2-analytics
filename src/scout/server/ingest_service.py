"""Server-side ingest API: receives presigned demo URLs from the local minter,
downloads them (fast, from inside the EU) and parses them into the shared cache.

The Streamlit website on the same box then serves the analytics from that cache.

Run:  uvicorn scout.server.ingest_service:app --host 0.0.0.0 --port 8600
Auth: whitelist your local IP(s) in SCOUT_ALLOWED_IPS (comma-separated). Use the
/whoami endpoint from your PC to discover the IP to whitelist. An optional
SCOUT_INGEST_TOKEN can be required on top.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from ..analytics.loader import parse_all
from ..ingest.faceit import _parsed_match_ids, download_demo
from ..paths import SCOUT_DIR

app = FastAPI(title="CS2 Scout ingest")
TOKEN = os.environ.get("SCOUT_INGEST_TOKEN")
ALLOWED_IPS = {ip.strip() for ip in os.environ.get("SCOUT_ALLOWED_IPS", "").split(",") if ip.strip()}


class Job(BaseModel):
    match_id: str
    url: str


class IngestRequest(BaseModel):
    jobs: list[Job]
    proxy: str | None = None


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")  # in case behind a reverse proxy
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _check_auth(request: Request, authorization: str | None) -> None:
    if ALLOWED_IPS and _client_ip(request) not in ALLOWED_IPS:
        raise HTTPException(status_code=403, detail=f"IP {_client_ip(request)} not whitelisted")
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing token")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "ip_whitelist": bool(ALLOWED_IPS), "token_required": bool(TOKEN)}


@app.get("/whoami")
def whoami(request: Request) -> dict:
    """The IP the server sees you from — whitelist this in SCOUT_ALLOWED_IPS. Open
    (no auth) so you can look it up before you're whitelisted."""
    return {"ip": _client_ip(request), "whitelisted": _client_ip(request) in ALLOWED_IPS}


@app.post("/ingest")
def ingest(req: IngestRequest, request: Request,
           authorization: str | None = Header(default=None)) -> dict:
    """Download (in parallel) any demos we don't have, then parse them (in parallel).

    Returns one result per job: {match_id, ok, cached, error}. Synchronous — the
    client should use a generous timeout (downloads + parse take a couple minutes).
    """
    _check_auth(request, authorization)
    SCOUT_DIR.mkdir(parents=True, exist_ok=True)
    have = _parsed_match_ids()
    results: dict[str, dict] = {}
    to_download = []
    for job in req.jobs:
        dest = SCOUT_DIR / f"{job.match_id}.dem"
        if dest.exists() or job.match_id in have:
            results[job.match_id] = {"match_id": job.match_id, "ok": True, "cached": True,
                                     "error": None}
        else:
            to_download.append(job)

    # download in parallel (per-connection throttling rarely matters from a server,
    # but parallel still helps and matches the local behaviour)
    def _dl(job: Job):
        dest = SCOUT_DIR / f"{job.match_id}.dem"
        try:
            download_demo(job.url, dest, proxy=req.proxy)
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

    # parse the freshly downloaded demos in parallel across cores (process pool)
    if downloaded:
        parse_all(SCOUT_DIR)
        now_parsed = _parsed_match_ids()
        for mid, _dest in downloaded:
            ok = mid in now_parsed
            results[mid] = {"match_id": mid, "ok": ok, "cached": False,
                            "error": None if ok else "parse failed"}

    ordered = [results.get(j.match_id, {"match_id": j.match_id, "ok": False,
                                        "error": "no result"}) for j in req.jobs]
    return {"results": ordered}

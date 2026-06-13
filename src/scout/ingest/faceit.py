"""FACEIT auto-scout: from a match room link, pull opponents' recent demos via the
official FACEIT Data API (no browser automation needed).

Requires a free API key: https://developers.faceit.com → create app → API key (server side).
"""
from __future__ import annotations

import gzip
import re
import shutil
from pathlib import Path
from typing import Callable

import requests
import zstandard

import json as _json

from ..paths import CACHE_ROOT, DATA_DIR, KEY_PATH, NICK_PATH, PROXY_PATH, SCOUT_DIR

API = "https://open.faceit.com/data/v4"
SIGN_URL = "https://www.faceit.com/api/download/v2/demos/download-url"
DEMO_CDN = "demos-europe-central.backblaze.faceit-cdn.net"
CURL_PATH = DATA_DIR / "faceit_curl.txt"  # captured browser request for presigning


def save_key(key: str) -> None:
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    KEY_PATH.write_text(key.strip())


def load_key() -> str | None:
    try:
        key = KEY_PATH.read_text().strip()
        return key or None
    except OSError:
        return None


def save_nick(nick: str) -> None:
    NICK_PATH.parent.mkdir(parents=True, exist_ok=True)
    NICK_PATH.write_text(nick.strip())


def load_nick() -> str | None:
    try:
        nick = NICK_PATH.read_text().strip()
        return nick or None
    except OSError:
        return None


def save_proxy(proxy: str) -> None:
    PROXY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROXY_PATH.write_text((proxy or "").strip())


def load_proxy() -> str | None:
    try:
        proxy = PROXY_PATH.read_text().strip()
        return proxy or None
    except OSError:
        return None


def _proxies(proxy: str | None) -> dict | None:
    """Build a requests proxies dict from a proxy URL, applied to demo downloads only."""
    if not proxy:
        return None
    proxy = proxy.strip()
    if "://" not in proxy:  # bare host:port → assume http
        proxy = "http://" + proxy
    return {"http": proxy, "https": proxy}


def test_cdn(proxy: str | None = None, sample_url: str | None = None, timeout: int = 20) -> tuple[bool, str]:
    """Quick reachability check for the demo CDN (optionally through a proxy).

    Returns (ok, human message). Any HTTP response means the host is reachable;
    only a connection/DNS failure means it's blocked.
    """
    url = sample_url or f"https://{DEMO_CDN}/cs2/connectivity-check.dem.zst"
    try:
        r = requests.get(url, headers={"Range": "bytes=0-1023"}, stream=True,
                         proxies=_proxies(proxy), timeout=timeout)
        code = r.status_code
        r.close()
        via = " (via proxy)" if proxy else ""
        if code in (200, 206):
            return True, f"✅ Reachable{via} — HTTP {code}. Downloads should work, re-run the scout."
        if code in (403, 404, 410):
            return True, (f"✅ Host reachable{via} (HTTP {code} on the test path is expected). "
                          "Your connection to the CDN works — re-run the scout.")
        return True, f"Host responded{via} with HTTP {code} — connection works."
    except requests.exceptions.ConnectionError:
        return False, ("❌ Still can't reach the CDN — this location is blocked. "
                       "Switch your VPN/proxy to another EU city (Amsterdam, Frankfurt) and retry.")
    except Exception as e:
        return False, f"❌ {type(e).__name__}: {e}"


def parse_match_id(url_or_id: str) -> str:
    m = re.search(r"(1-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  url_or_id.strip())
    return m.group(1) if m else url_or_id.strip()


def match_room_url(match_id: str) -> str:
    """The FACEIT match room page — where a logged-in browser can download the demo."""
    return f"https://www.faceit.com/en/cs2/room/{match_id}"


# --- experimental auto-download via the user's captured browser request ----------

def save_curl(curl: str) -> None:
    CURL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURL_PATH.write_text((curl or "").strip(), encoding="utf-8")


def load_curl() -> str | None:
    try:
        text = CURL_PATH.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def parse_curl(curl: str) -> dict:
    """Parse a 'Copy as cURL' command into {url, headers, cookie}.

    Handles both single- and double-quoted forms (bash & cmd/PowerShell copies).
    """
    headers: dict[str, str] = {}
    # -H 'Key: Value'  or  -H "Key: Value"
    for m in re.finditer(r"""-H\s+(['"])(.*?)\1""", curl, re.S):
        raw = m.group(2)
        if ":" in raw:
            k, v = raw.split(":", 1)
            headers[k.strip()] = v.strip()
    # -b / --cookie 'name=val; ...'
    cm = re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", curl, re.S)
    if cm:
        headers.setdefault("Cookie", cm.group(2).strip())
    # the request URL
    um = re.search(r"""curl\s+(?:-[A-Za-z]+\s+)*(['"]?)(https?://[^\s'"]+)\1""", curl)
    url = um.group(2) if um else None
    return {"url": url, "headers": headers}


def _find_presigned(obj) -> str | None:
    """Recursively find a presigned demo URL in a JSON response."""
    if isinstance(obj, str):
        if obj.startswith("http") and ("X-Amz" in obj or "backblazeb2.com" in obj
                                        or "faceit-cdn" in obj or "Authorization=" in obj):
            return obj
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_presigned(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _find_presigned(v)
            if found:
                return found
    return None


def presign_demo_url(resource_url: str, curl_text: str, timeout: int = 30) -> str:
    """Replay the captured browser request to get a downloadable presigned URL.

    Reuses the captured headers (auth token + cookies + cf_clearance + UA) and
    swaps in resource_url. Must run on the same machine/IP whose browser produced
    the capture, or Cloudflare will re-challenge.
    """
    parsed = parse_curl(curl_text)
    endpoint = parsed["url"] or SIGN_URL
    headers = {k: v for k, v in parsed["headers"].items()
               if k.lower() not in ("content-length", "accept-encoding")}
    headers.setdefault("Content-Type", "application/json")
    r = requests.post(endpoint, data=_json.dumps({"resource_url": resource_url}),
                      headers=headers, timeout=timeout)
    if r.headers.get("cf-mitigated") == "challenge" or "Just a moment" in r.text[:300]:
        raise RuntimeError(
            "Cloudflare re-challenged the request. Re-copy the cURL from your browser "
            "(the cf_clearance cookie expires), and make sure this app runs on the same PC."
        )
    if r.status_code in (401, 403):
        raise RuntimeError(f"FACEIT rejected the session ({r.status_code}). Re-copy the cURL "
                           "while logged in.")
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Unexpected response: {r.text[:160]}")
    url = _find_presigned(data)
    if not url:
        raise RuntimeError(f"No download URL in response: {str(data)[:160]}")
    return url


def test_session(curl_text: str, timeout: int = 30) -> tuple[bool, str]:
    """Check whether a captured browser request gets past Cloudflare + auth.

    Uses a throwaway resource_url: we only care that the request isn't bounced by
    Cloudflare or rejected as unauthenticated, which proves the session is usable.
    """
    parsed = parse_curl(curl_text)
    if not parsed["headers"]:
        return False, "Couldn't read any headers from that cURL — re-copy it as 'cURL (bash)'."
    endpoint = parsed["url"] or SIGN_URL
    headers = {k: v for k, v in parsed["headers"].items()
               if k.lower() not in ("content-length", "accept-encoding")}
    headers.setdefault("Content-Type", "application/json")
    try:
        r = requests.post(endpoint,
                          data=_json.dumps({"resource_url": f"https://{DEMO_CDN}/cs2/x.dem.zst"}),
                          headers=headers, timeout=timeout)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.headers.get("cf-mitigated") == "challenge" or "Just a moment" in r.text[:300]:
        return False, ("Cloudflare blocked it — the cf_clearance cookie expired or your IP "
                       "differs. Re-copy the cURL in your browser and run this app on that same PC.")
    if r.status_code in (401, 403):
        return False, f"Auth rejected ({r.status_code}) — re-copy the cURL while logged in to FACEIT."
    return True, f"Session works (HTTP {r.status_code}) — auto-download is live. Run the scout."


def _get(path: str, key: str, **params) -> dict:
    r = requests.get(API + path, headers={"Authorization": f"Bearer {key}"},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def match_details(match_id: str, key: str) -> dict:
    return _get(f"/matches/{match_id}", key)


def rosters(details: dict) -> dict[str, list[dict]]:
    """faction name -> [{nickname, player_id, steamid}] from match details."""
    out = {}
    for faction in ("faction1", "faction2"):
        team = details.get("teams", {}).get(faction)
        if not team:
            continue
        players = [
            {
                "nickname": p.get("nickname"),
                "player_id": p.get("player_id"),
                "steamid": str(p.get("game_player_id", "")),
            }
            for p in team.get("roster", [])
        ]
        out[team.get("name", faction)] = players
    return out


def find_sides(details: dict, my_nick: str) -> dict | None:
    """Given a match and your nickname, return which team is yours and which is the enemy.

    Returns {my_team, enemy_team, enemy_players, my_players} or None if the nickname
    isn't on either roster (case-insensitive match).
    """
    teams = rosters(details)
    if not my_nick:
        return None
    target = my_nick.strip().lower()
    team_names = list(teams)
    for i, name in enumerate(team_names):
        if any((p.get("nickname") or "").lower() == target for p in teams[name]):
            enemy_name = team_names[1 - i] if len(team_names) == 2 else None
            return {
                "my_team": name,
                "enemy_team": enemy_name,
                "my_players": teams[name],
                "enemy_players": teams[enemy_name] if enemy_name else [],
            }
    return None


def match_map(details: dict) -> str | None:
    try:
        pick = details["voting"]["map"]["pick"]
        return pick[0] if isinstance(pick, list) else pick
    except (KeyError, IndexError, TypeError):
        return None


def player_history(player_id: str, key: str, limit: int = 20) -> list[str]:
    data = _get(f"/players/{player_id}/history", key, game="cs2", limit=limit)
    return [item["match_id"] for item in data.get("items", [])]


def signed_demo_url(resource_url: str) -> str:
    """FACEIT demo links need signing; falls back to the raw URL if signing fails."""
    try:
        r = requests.post(SIGN_URL, json={"resource_url": resource_url}, timeout=30)
        r.raise_for_status()
        return r.json()["payload"]["download_url"]
    except Exception:
        return resource_url


GZIP_MAGIC = b"\x1f\x8b"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DEM_MAGIC = b"HL2DEMO"  # CS2 demo header
ZSTD_MAX = (1 << 31) - 1  # max window the zstd decoder allows (~2 GB); covers --long frames


def _decompress(tmp: Path, dest: Path) -> None:
    """Decompress tmp into dest based on magic bytes (FACEIT uses .zst, sometimes .gz)."""
    with open(tmp, "rb") as f:
        head = f.read(8)
    if head.startswith(ZSTD_MAGIC):
        dctx = zstandard.ZstdDecompressor(max_window_size=ZSTD_MAX)
        with open(tmp, "rb") as src, open(dest, "wb") as out:
            dctx.copy_stream(src, out)
        tmp.unlink()
    elif head.startswith(GZIP_MAGIC):
        with gzip.open(tmp, "rb") as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        tmp.unlink()
    elif head.startswith(DEM_MAGIC):  # already a raw .dem
        tmp.replace(dest)
    else:
        # Unknown — keep bytes so the error is visible to the user rather than silent.
        tmp.replace(dest)
        raise ValueError(
            f"Downloaded file is neither zstd/gzip nor a raw demo (magic={head[:4]!r}). "
            "FACEIT may have returned an error page instead of the demo."
        )


def decompress_to_dem(src: str | Path) -> Path:
    """Decompress a manually-downloaded .dem.zst / .dem.gz file to a sibling .dem.

    Returns the .dem path. If src is already a raw demo, returns it unchanged.
    """
    src = Path(src)
    name = src.name
    for ext in (".zst", ".gz"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name.lower().endswith(".dem"):
        name += ".dem"
    dest = src.with_name(name)
    with open(src, "rb") as f:
        head = f.read(8)
    if head.startswith(DEM_MAGIC):
        return src
    if head.startswith(ZSTD_MAGIC):
        dctx = zstandard.ZstdDecompressor(max_window_size=ZSTD_MAX)
        with open(src, "rb") as s, open(dest, "wb") as o:
            dctx.copy_stream(s, o)
    elif head.startswith(GZIP_MAGIC):
        with gzip.open(src, "rb") as s, open(dest, "wb") as o:
            shutil.copyfileobj(s, o)
    else:
        raise ValueError(f"{src.name} is not a demo or a zstd/gzip archive.")
    return dest


def prepare_compressed_demos(root: str | Path) -> list[Path]:
    """Decompress any *.dem.zst / *.dem.gz under root whose .dem isn't already present.

    Lets users drop browser-downloaded compressed demos straight into data/demos.
    Returns the list of newly produced .dem paths.
    """
    root = Path(root)
    out: list[Path] = []
    if not root.exists():
        return out
    for pattern in ("*.dem.zst", "*.dem.gz"):
        for comp in root.rglob(pattern):
            dem = comp.with_name(comp.name.rsplit(".", 1)[0])  # drop .zst/.gz
            if dem.exists():
                continue
            try:
                out.append(decompress_to_dem(comp))
            except Exception:
                continue
    return out


def download_demo(
    url: str,
    dest: Path,
    chunk: int = 1 << 20,
    on_bytes: Callable[[int, int], None] | None = None,
    proxy: str | None = None,
) -> Path:
    """Stream and decompress a FACEIT demo (.zst / .gz / raw) to dest .dem path.

    Tries the Data API's direct demo_url first; if it's expired (403/410), retries
    once via the website signing endpoint. on_bytes(downloaded, total) reports
    transfer progress (total is 0 if the server omits Content-Length). proxy, if
    set, routes only this CDN transfer (never the API calls, so the key stays off it).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    proxies = _proxies(proxy)

    def _stream(u: str, attempts: int = 3) -> int:
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                with requests.get(u, stream=True, timeout=300, proxies=proxies) as r:
                    if r.status_code in (403, 410):
                        return r.status_code
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0) or 0)
                    done = 0
                    with open(tmp, "wb") as f:
                        for part in r.iter_content(chunk_size=chunk):
                            f.write(part)
                            done += len(part)
                            if on_bytes:
                                on_bytes(done, total)
                return 200
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as e:
                last_exc = e  # transient on long-distance links — retry from scratch
        raise ConnectionError(
            f"FACEIT's demo link ({u.split('/')[2]}) isn't directly downloadable — the host "
            "isn't publicly resolvable and the file needs a browser-signed URL. Download it from "
            "the match room in your browser instead."
        ) from last_exc

    status = _stream(url)
    if status in (403, 410):
        signed = signed_demo_url(url)
        if signed != url:
            status = _stream(signed)
        if status in (403, 410):
            raise FileNotFoundError(
                f"Demo link expired or unavailable ({status}) — FACEIT keeps CS2 demos "
                "for a limited window, so older matches may no longer be downloadable."
            )
    _decompress(tmp, dest)
    return dest


def discover_matches(
    match_id: str,
    key: str,
    enemy_players: list[dict],
    per_player: int = 1,
    map_filter: str | None = None,
    history_depth: int = 20,
    progress: Callable[[str], None] | None = None,
    emit: Callable[[dict], None] | None = None,
) -> tuple[list[dict], dict]:
    """Find scoutable matches for the enemy players.

    Walks each player's history newest-first, dedupes matches the team shares,
    and (when map_filter is set) digs past non-matching maps to find demos on the
    target map. Returns (candidates, details_cache). Each candidate:
    {match_id, nickname, map, details}.
    """
    say = progress or (lambda _msg: None)
    emit = emit or (lambda _info: None)
    seen: set[str] = {match_id}
    details_cache: dict[str, dict] = {}
    candidates: list[dict] = []

    for p in enemy_players:
        target = f"{map_filter} " if map_filter else ""
        say(f"finding {target}matches for {p['nickname']} …")
        try:
            hist = player_history(p["player_id"], key, limit=history_depth)
        except Exception as e:
            say(f"  history failed for {p['nickname']}: {e}")
            continue
        found = checked = 0
        for mid in hist:
            if found >= per_player:
                break
            if mid in seen:
                continue
            seen.add(mid)
            if not map_filter:
                candidates.append({"match_id": mid, "nickname": p["nickname"],
                                   "map": None, "details": None})
                found += 1
                continue
            # map filter set: need the map, which only match_details has
            try:
                det = details_cache.get(mid) or match_details(mid, key)
            except Exception as e:
                say(f"  couldn't read a match for {p['nickname']}: {e}")
                continue
            details_cache[mid] = det
            checked += 1
            mmap = match_map(det)
            emit({"phase": "searching", "nickname": p["nickname"], "checked": checked,
                  "map": mmap, "index": 0, "count": 0, "match_id": mid,
                  "downloaded": 0, "total": 0})
            if mmap != map_filter:
                continue
            candidates.append({"match_id": mid, "nickname": p["nickname"],
                               "map": mmap, "details": det})
            found += 1
        if map_filter and found == 0:
            say(f"  no {map_filter} demo found for {p['nickname']} in last {history_depth} games")
    return candidates, details_cache


def scout_opponents(
    match_id: str,
    key: str,
    enemy_players: list[dict],
    per_player: int = 1,
    map_filter: str | None = None,
    total_cap: int = 8,
    history_depth: int = 20,
    proxy: str | None = None,
    curl_session: str | None = None,
    progress: Callable[[str], None] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Find and download scoutable demos for the enemy players. Returns a report dict.

    If curl_session (a captured browser 'Copy as cURL') is given, demos are presigned
    through it and auto-downloaded; otherwise the report just returns match-room links.

    progress(msg) gets narration lines; on_progress(info) gets structured events:
    {phase, match_id, nickname, index, count, map, downloaded, total}.
    """
    say = progress or (lambda _msg: None)
    emit = on_progress or (lambda _info: None)

    candidates, details_cache = discover_matches(
        match_id, key, enemy_players, per_player=per_player,
        map_filter=map_filter, history_depth=history_depth, progress=say, emit=emit,
    )
    say(f"{len(candidates)} demo(s) to fetch.")

    # FACEIT serves demos only through the logged-in browser, so the reliable path is the
    # match-room page. Collect a room link (+ the raw url for reference) per found demo.
    links = []
    for cand in candidates:
        det = cand["details"] or details_cache.get(cand["match_id"])
        urls = (det.get("demo_url") if det else None) or []
        links.append({"match_id": cand["match_id"], "nickname": cand["nickname"],
                      "map": cand["map"], "url": urls[0] if urls else None,
                      "room": match_room_url(cand["match_id"])})

    downloaded, skipped, errors = [], [], []
    count = len(candidates)
    for idx, cand in enumerate(candidates, 1):
        mid, who = cand["match_id"], cand["nickname"]
        base = {"match_id": mid, "nickname": who, "index": idx, "count": count}
        if len(downloaded) >= total_cap:
            skipped.append((mid, "total cap reached"))
            continue
        dest = SCOUT_DIR / f"{mid}.dem"
        if dest.exists():
            emit({**base, "phase": "cached", "downloaded": 0, "total": 0, "map": cand["map"]})
            downloaded.append({"match_id": mid, "file": dest, "cached": True})
            continue
        try:
            det = cand["details"] or details_cache.get(mid) or match_details(mid, key)
            mmap = cand["map"] or match_map(det)
            urls = det.get("demo_url") or []
            if not urls:
                skipped.append((mid, "no demo available"))
                emit({**base, "phase": "skipped", "downloaded": 0, "total": 0, "map": mmap})
                continue
            if not curl_session:
                # no browser session captured → can't presign; leave for the room-link fallback
                skipped.append((mid, "needs browser session"))
                continue
            say(f"presigning + downloading demo {idx}/{count} — {who}'s match ({mmap}) …")
            emit({**base, "phase": "start", "downloaded": 0, "total": 0, "map": mmap})
            dl_url = presign_demo_url(urls[0], curl_session)

            def _cb(done: int, total: int, _b=base, _m=mmap) -> None:
                emit({**_b, "phase": "downloading", "downloaded": done, "total": total, "map": _m})

            download_demo(dl_url, dest, on_bytes=_cb, proxy=proxy)
            emit({**base, "phase": "done", "downloaded": 0, "total": 0, "map": mmap})
            downloaded.append({"match_id": mid, "file": dest, "map": mmap, "cached": False})
        except Exception as e:
            errors.append((mid, f"{type(e).__name__}: {e}"))
            emit({**base, "phase": "error", "downloaded": 0, "total": 0, "map": None})
    return {"downloaded": downloaded, "skipped": skipped, "errors": errors, "links": links}


def _parsed_match_ids() -> set[str]:
    """FACEIT match ids we've already parsed (by the {match_id}.dem source name in
    each cache header) — so we skip re-downloading even if the .dem was deleted."""
    out: set[str] = set()
    if not CACHE_ROOT.exists():
        return out
    for d in CACHE_ROOT.iterdir():
        header = d / "header.json"
        if not header.is_file():
            continue
        try:
            src = _json.loads(header.read_text()).get("source_filename", "")
        except (OSError, ValueError):
            continue
        if src.endswith(".dem"):
            out.add(src[:-4])
    return out


def scout_opponents_browser(
    match_id: str,
    key: str,
    enemy_players: list[dict],
    per_player: int = 1,
    map_filter: str | None = None,
    total_cap: int = 8,
    history_depth: int = 20,
    proxy: str | None = None,
    progress: Callable[[str], None] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Same as scout_opponents, but uses the logged-in Chrome worker to presign each
    demo, then reuses download_demo() to fetch it. Report shape and on_progress
    events match scout_opponents exactly, so the UI is unchanged.
    """
    from . import browser_runner

    say = progress or (lambda _msg: None)
    emit = on_progress or (lambda _info: None)

    candidates, details_cache = discover_matches(
        match_id, key, enemy_players, per_player=per_player,
        map_filter=map_filter, history_depth=history_depth, progress=say, emit=emit,
    )
    say(f"{len(candidates)} demo(s) to fetch.")

    downloaded, skipped, errors = [], [], []
    links, items, meta = [], [], {}
    count = len(candidates)
    already_parsed = _parsed_match_ids()
    for idx, cand in enumerate(candidates, 1):
        mid = cand["match_id"]
        meta[mid] = {"nickname": cand["nickname"], "map": cand["map"], "index": idx}
        base = {**meta[mid], "match_id": mid, "count": count}
        # already have this demo (on disk OR already parsed)? skip the browser +
        # the 300 MB download entirely.
        dest = SCOUT_DIR / f"{mid}.dem"
        if dest.exists() or mid in already_parsed:
            downloaded.append({"match_id": mid, "file": dest, "map": cand["map"], "cached": True})
            say(f"already have {cand['nickname']}'s demo — skipping download.")
            emit({**base, "phase": "cached", "downloaded": 0, "total": 0})
            continue
        det = cand["details"] or details_cache.get(mid)
        if det is None:
            try:
                det = match_details(mid, key)
            except Exception:
                det = None
        urls = (det.get("demo_url") if det else None) or []
        links.append({"match_id": mid, "nickname": cand["nickname"], "map": cand["map"],
                      "url": urls[0] if urls else None, "room": match_room_url(mid)})
        if urls:
            items.append(f"{mid}={urls[0]}")

    if not items:
        # nothing new to fetch (all already on disk, or no demo links)
        return {"downloaded": downloaded, "skipped": skipped, "errors": errors, "links": links}

    # Phase 1: sign all demo links in the browser (fast). Collect presigned URLs.
    say("Opening logged-in Chrome to sign the demo links…")
    presigned = []  # (mid, url, base)
    for ev in browser_runner.run("download", *items):
        phase = ev.get("phase")
        mid = ev.get("match_id")
        info = meta.get(mid, {})
        base = {"match_id": mid, "nickname": info.get("nickname"), "index": info.get("index"),
                "count": count, "map": info.get("map")}
        if phase == "needs_login":
            errors.append((match_id, ev.get("msg", "not logged in")))
            say("⚠️ " + ev.get("msg", "not logged in"))
            break
        if phase in ("log", "launching", "awaiting_login"):
            say(ev.get("msg", phase))
        elif phase == "presigned":
            presigned.append((mid, ev["url"], base))
        elif phase == "error":
            reason = (f"Cloudflare/auth ({ev.get('status')}) — re-run Log in"
                      if ev.get("challenged") else ev.get("msg", "presign failed"))
            errors.append((mid, reason))
            emit({**base, "phase": "error", "downloaded": 0, "total": 0})
        elif phase in ("fatal", "exit_error"):
            errors.append((match_id, ev.get("msg", f"worker exit {ev.get('code')}")))

    # Phase 2: download the signed links in PARALLEL (per-connection throttling means
    # several at once is far faster than one at a time on a long-distance link).
    if presigned:
        say(f"downloading {len(presigned)} demo(s) in parallel…")
        _parallel_download(presigned, downloaded, errors, emit, proxy)

    return {"downloaded": downloaded, "skipped": skipped, "errors": errors, "links": links}


def _parallel_download(presigned, downloaded, errors, emit, proxy, max_workers=4):
    """Download presigned demo URLs concurrently. Download threads only update a
    shared byte-counter (under a lock); the calling thread polls it and emits the
    aggregate progress (Streamlit widget updates must stay on the calling thread)."""
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    state = {mid: {"done": 0, "total": 0} for mid, _, _ in presigned}
    lock = threading.Lock()

    def worker(mid: str, url: str):
        dest = SCOUT_DIR / f"{mid}.dem"

        def cb(done: int, total: int) -> None:
            with lock:
                state[mid]["done"] = done
                state[mid]["total"] = total

        download_demo(url, dest, on_bytes=cb, proxy=proxy)
        return mid, dest

    n = len(presigned)
    with ThreadPoolExecutor(max_workers=min(max_workers, n)) as ex:
        futures = {ex.submit(worker, mid, url): base for mid, url, base in presigned}
        while not all(f.done() for f in futures):
            with lock:
                td = sum(s["done"] for s in state.values())
                tt = sum(s["total"] for s in state.values())
            emit({"phase": "downloading_parallel", "count": n, "downloaded": td, "total": tt})
            time.sleep(0.5)
        for fut, base in futures.items():
            try:
                mid, dest = fut.result()
                downloaded.append({"match_id": mid, "file": dest, "map": base.get("map"),
                                   "cached": False})
                emit({**base, "phase": "done", "downloaded": 0, "total": 0})
            except Exception as e:
                errors.append((base["match_id"], f"{type(e).__name__}: {e}"))
                emit({**base, "phase": "error", "downloaded": 0, "total": 0})

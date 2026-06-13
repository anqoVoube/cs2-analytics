from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from ..parse import cache_dir, hash_demo, is_cached, parse_demo
from ..paths import DEMOS_DIR, INDEX_PATH

TABLES = ("rounds", "deaths", "bombs", "ticks", "damages", "shots", "util", "blinds", "econ")

T_SIDE = 2
CT_SIDE = 3
SIDE_NAME = {2: "T", 3: "CT"}


@dataclass
class Match:
    demo_hash: str
    header: dict
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def map_name(self) -> str:
        return self.header.get("map_name", "unknown")

    @property
    def label(self) -> str:
        return Path(self.header.get("source_filename", self.demo_hash[:8])).stem


def iter_demo_files(root: str | Path = DEMOS_DIR) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(root.rglob("*.dem"))


def _load_index() -> dict:
    try:
        return json.loads(INDEX_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def fast_hash(path: str | Path) -> str:
    """SHA-256 of a demo, memoized by (size, mtime) in a sidecar index.

    Avoids re-reading multi-hundred-MB files on every UI interaction.
    """
    path = Path(path)
    idx = _load_index()
    key = str(path.resolve())
    stat = path.stat()
    rec = idx.get(key)
    if rec and rec.get("size") == stat.st_size and rec.get("mtime") == int(stat.st_mtime):
        return rec["hash"]
    h = hash_demo(path)
    idx[key] = {"size": stat.st_size, "mtime": int(stat.st_mtime), "hash": h}
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(idx, indent=1))
    return h


def demo_status(root: str | Path = DEMOS_DIR) -> pd.DataFrame:
    """One row per .dem file: folder, size, parsed state, map and round count if cached."""
    rows = []
    root = Path(root)
    for demo in iter_demo_files(root):
        h = fast_hash(demo)
        cached = is_cached(h)
        map_name = rounds = None
        if cached:
            try:
                header = json.loads((cache_dir(h) / "header.json").read_text())
                map_name = header.get("map_name")
                rounds = len(pd.read_parquet(cache_dir(h) / "rounds.parquet"))
            except (OSError, json.JSONDecodeError):
                cached = False
        rows.append(
            {
                "file": demo.name,
                "folder": str(demo.parent.relative_to(root)) if demo.parent != root else ".",
                "size MB": round(demo.stat().st_size / 1e6, 1),
                "parsed": cached,
                "map": map_name,
                "rounds": rounds,
                "hash": h,
            }
        )
    return pd.DataFrame(rows)


def load_match(demo_hash: str) -> Match:
    d = cache_dir(demo_hash)
    header = json.loads((d / "header.json").read_text())
    tables = {}
    for name in TABLES:
        path = d / f"{name}.parquet"
        tables[name] = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    return Match(demo_hash=demo_hash, header=header, tables=tables)


def parse_all(
    root: str | Path = DEMOS_DIR,
    force: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[dict]:
    """Parse every .dem under root (skipping already-cached). Returns parse result dicts."""
    demos = iter_demo_files(root)
    results = []
    for i, demo in enumerate(demos):
        if progress:
            progress(i, len(demos), demo.name)
        try:
            h = fast_hash(demo)
            if not force and is_cached(h):
                res = {"hash": h, "cached": True, "file": str(demo), "error": None}
            else:
                res = parse_demo(demo, force=force)
                res["file"] = str(demo)
                res["error"] = None
        except Exception as e:  # a corrupt demo should not kill the batch
            res = {"file": str(demo), "error": f"{type(e).__name__}: {e}"}
        results.append(res)
    if progress:
        progress(len(demos), len(demos), "done")
    return results


class MatchSet:
    """Stack of parsed matches with per-table DataFrames carrying demo provenance columns."""

    def __init__(self, matches: list[Match]):
        self.matches = matches
        self.by_hash = {m.demo_hash: m for m in matches}
        for name in TABLES:
            frames = []
            for m in matches:
                df = m.tables.get(name)
                if df is None or df.empty:
                    continue
                df = df.copy()
                if "total_rounds_played" in df.columns and "round_idx" not in df.columns:
                    df = df.rename(columns={"total_rounds_played": "round_idx"})
                df["demo_hash"] = m.demo_hash
                df["map_name"] = m.map_name
                df["demo_label"] = m.label
                frames.append(df)
            stacked = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
            setattr(self, name, stacked)

    def filtered(self, map_name: str | None = None) -> "MatchSet":
        if not map_name:
            return self
        return MatchSet([m for m in self.matches if m.map_name == map_name])

    def maps(self) -> list[str]:
        return sorted({m.map_name for m in self.matches})

    def roster(self) -> pd.DataFrame:
        """All players seen across matches: steamid, most recent name, matches and rounds played."""
        if self.econ.empty:
            return pd.DataFrame(columns=["steamid", "name", "matches", "rounds"])
        e = self.econ
        agg = (
            e.groupby("steamid")
            .agg(
                name=("name", lambda s: s.iloc[-1]),
                matches=("demo_hash", "nunique"),
                rounds=("round_idx", "count"),
            )
            .reset_index()
            .sort_values(["matches", "rounds"], ascending=False)
            .reset_index(drop=True)
        )
        return agg

    def player_maps(self, steamid: str) -> list[str]:
        if self.econ.empty:
            return []
        mine = self.econ[self.econ["steamid"] == str(steamid)]
        return sorted(mine["map_name"].unique())


def cached_hashes() -> list[str]:
    """Hashes of every fully-parsed demo in the cache.

    Source of truth for analysis: a demo is loadable once parsed, independent of
    whether its (large) .dem file still exists on disk.
    """
    from ..parse.cache import CACHE_ROOT

    if not CACHE_ROOT.exists():
        return []
    return sorted(d.name for d in CACHE_ROOT.iterdir() if d.is_dir() and is_cached(d.name))


def load_matchset(root: str | Path = DEMOS_DIR) -> MatchSet:
    """Load every parsed demo from the cache into a MatchSet (does not parse).

    Loads from the parquet cache directly, so analysis survives even if the .dem
    file was deleted/moved after parsing.
    """
    return MatchSet([load_match(h) for h in cached_hashes()])

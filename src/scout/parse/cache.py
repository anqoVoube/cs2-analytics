from __future__ import annotations

import json

from ..paths import CACHE_ROOT

CACHE_VERSION = 3

ARTIFACTS = (
    "header.json",
    "players.parquet",
    "rounds.parquet",
    "deaths.parquet",
    "bombs.parquet",
    "ticks.parquet",
    "damages.parquet",
    "shots.parquet",
    "util.parquet",
    "blinds.parquet",
    "econ.parquet",
)


def cache_dir(demo_hash: str) -> Path:
    return CACHE_ROOT / demo_hash


def is_cached(demo_hash: str) -> bool:
    d = cache_dir(demo_hash)
    if not all((d / name).exists() for name in ARTIFACTS):
        return False
    try:
        header = json.loads((d / "header.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return header.get("cache_version") == CACHE_VERSION

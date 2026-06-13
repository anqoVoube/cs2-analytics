from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from demoparser2 import DemoParser

from .cache import CACHE_VERSION, cache_dir, is_cached
from .hashing import hash_demo

TICKRATE = 64  # CS2 demos are 64 ticks/s
TICK_SAMPLE_STEP = 32  # sample player positions every ~0.5 s
ECON_SAMPLE_OFFSET = 5 * TICKRATE  # equip value read 5 s into the round (after late buys)

TICK_PROPS = ["X", "Y", "Z", "last_place_name", "is_alive", "health", "team_num"]

UTIL_EVENTS = (
    "smokegrenade_detonate",
    "flashbang_detonate",
    "hegrenade_detonate",
    "inferno_startburn",
    "decoy_detonate",
)


def _event_df(parser: DemoParser, event: str, **kwargs) -> pd.DataFrame:
    """parse_event that always returns a DataFrame (demoparser2 returns [] when empty)."""
    try:
        out = parser.parse_event(event, **kwargs)
    except Exception:
        return pd.DataFrame()
    return out if isinstance(out, pd.DataFrame) else pd.DataFrame()


def _str_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize every *steamid* column to plain strings so joins work across tables."""
    for col in df.columns:
        if "steamid" in col:
            df[col] = df[col].astype("string").astype(str)
    return df


def _warmup_end_tick(parser: DemoParser) -> int:
    """First tick where is_warmup_period is False. Events before this are warmup noise."""
    ticks = parser.parse_ticks(["is_warmup_period"])
    live = ticks.loc[~ticks["is_warmup_period"], "tick"]
    return int(live.iloc[0]) if len(live) else 0


def _round_table(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    """One row per round: idx, start_tick, end_tick. Derived from total_rounds_played transitions."""
    ticks = parser.parse_ticks(["total_rounds_played", "is_warmup_period"])
    live = ticks[(~ticks["is_warmup_period"]) & (ticks["tick"] >= warmup_end)].copy()
    live = live.drop_duplicates(subset=["tick"]).sort_values("tick")

    first_ticks = live.groupby("total_rounds_played", as_index=False)["tick"].min()
    first_ticks = first_ticks.sort_values("total_rounds_played").reset_index(drop=True)
    first_ticks = first_ticks.rename(columns={"total_rounds_played": "round_idx", "tick": "start_tick"})
    first_ticks["end_tick"] = first_ticks["start_tick"].shift(-1) - 1
    last_tick = int(live["tick"].max())
    first_ticks.loc[first_ticks.index[-1], "end_tick"] = last_tick
    for col in ("round_idx", "start_tick", "end_tick"):
        first_ticks[col] = first_ticks[col].astype("int64")
    return first_ticks[["round_idx", "start_tick", "end_tick"]]


def _first_in_range(ticks: pd.Series, start: int, end: int) -> int | None:
    hit = ticks[(ticks >= start) & (ticks <= end)]
    return int(hit.iloc[0]) if len(hit) else None


def _site_letter(place: object) -> str:
    name = str(place or "").lower()
    if "bombsitea" in name or name.endswith("a"):
        return "A"
    if "bombsiteb" in name or name.endswith("b"):
        return "B"
    return "?"


def _enrich_rounds(parser: DemoParser, rounds: pd.DataFrame, bombs: pd.DataFrame) -> pd.DataFrame:
    """Attach freeze_end_tick, winner/reason, and bomb plant info to each round."""
    freeze = _event_df(parser, "round_freeze_end")
    freeze_ticks = freeze["tick"].sort_values() if "tick" in freeze else pd.Series(dtype="int64")
    ends = _event_df(parser, "round_end")

    plants = pd.DataFrame()
    if len(bombs):
        plants = bombs[bombs["event"] == "bomb_planted"]

    rows = []
    for r in rounds.itertuples(index=False):
        fe = _first_in_range(freeze_ticks, r.start_tick, r.end_tick)
        winner, reason = None, None
        if len(ends):
            # round_end fires on the first tick of the *next* round, hence end_tick + 1.
            in_range = ends[(ends["tick"] >= r.start_tick) & (ends["tick"] <= r.end_tick + 1)]
            if len(in_range):
                last = in_range.iloc[-1]
                winner = str(last.get("winner")) if last.get("winner") is not None else None
                reason = str(last.get("reason")) if last.get("reason") is not None else None
        plant_tick, plant_site = None, None
        if len(plants):
            in_range = plants[(plants["tick"] >= r.start_tick) & (plants["tick"] <= r.end_tick)]
            if len(in_range):
                first = in_range.iloc[0]
                plant_tick = int(first["tick"])
                plant_site = _site_letter(first.get("user_last_place_name"))
        rows.append(
            {
                "round_idx": r.round_idx,
                "start_tick": r.start_tick,
                "freeze_end_tick": fe if fe is not None else r.start_tick,
                "end_tick": r.end_tick,
                "winner": winner,
                "reason": reason,
                "plant_tick": plant_tick,
                "plant_site": plant_site,
            }
        )
    out = pd.DataFrame(rows)
    out["plant_tick"] = out["plant_tick"].astype("Int64")
    # Rounds with no round_end are aborted rounds or post-match leftovers — drop them
    # so they don't pollute win rates, presence maps, and tactic books.
    out = out[out["winner"].notna()].reset_index(drop=True)
    return out


def _deaths(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    df = _event_df(
        parser,
        "player_death",
        player=["X", "Y", "Z", "team_num", "last_place_name"],
        other=["total_rounds_played", "is_bomb_planted"],
    )
    if df.empty:
        return df
    return df[df["tick"] >= warmup_end].reset_index(drop=True)


def _bombs(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    frames = []
    for event in ("bomb_planted", "bomb_defused", "bomb_exploded"):
        df = _event_df(
            parser,
            event,
            player=["X", "Y", "Z", "team_num", "last_place_name"],
            other=["total_rounds_played"],
        )
        if df.empty:
            continue
        df = df.copy()
        df["event"] = event
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    return merged[merged["tick"] >= warmup_end].reset_index(drop=True)


def _damages(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    df = _event_df(
        parser,
        "player_hurt",
        player=["team_num"],
        other=["total_rounds_played", "is_bomb_planted"],
    )
    if df.empty:
        return df
    return df[df["tick"] >= warmup_end].reset_index(drop=True)


def _shots(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    df = _event_df(parser, "weapon_fire", other=["total_rounds_played"])
    if df.empty:
        return df
    return df[df["tick"] >= warmup_end].reset_index(drop=True)


def _util(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    frames = []
    for event in UTIL_EVENTS:
        df = _event_df(
            parser, event, player=["team_num", "X", "Y"], other=["total_rounds_played"]
        )
        if df.empty:
            continue
        df = df.copy()
        df["event"] = event
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged = merged[merged["tick"] >= warmup_end].reset_index(drop=True)
    renames = {
        "user_steamid": "thrower_steamid",
        "user_name": "thrower_name",
        "user_team_num": "thrower_team_num",
        "user_X": "thrower_X",
        "user_Y": "thrower_Y",
    }
    return merged.rename(columns={k: v for k, v in renames.items() if k in merged.columns})


def _blinds(parser: DemoParser, warmup_end: int) -> pd.DataFrame:
    df = _event_df(
        parser, "player_blind", player=["team_num"], other=["total_rounds_played"]
    )
    if df.empty:
        return df
    return df[df["tick"] >= warmup_end].reset_index(drop=True)


def _sampled_ticks(parser: DemoParser, rounds: pd.DataFrame) -> pd.DataFrame:
    """Player positions sampled every TICK_SAMPLE_STEP ticks between freeze end and round end."""
    wanted: list[int] = []
    tick_round: dict[int, int] = {}
    tick_secs: dict[int, float] = {}
    for r in rounds.itertuples(index=False):
        fe = int(r.freeze_end_tick)
        for t in range(fe, int(r.end_tick) + 1, TICK_SAMPLE_STEP):
            wanted.append(t)
            tick_round[t] = int(r.round_idx)
            tick_secs[t] = (t - fe) / TICKRATE
    if not wanted:
        return pd.DataFrame()
    df = parser.parse_ticks(TICK_PROPS, ticks=wanted)
    if df.empty:
        return df
    df = df.copy()
    df["round_idx"] = df["tick"].map(tick_round).astype("int64")
    df["secs"] = df["tick"].map(tick_secs).astype("float64")
    df["last_place_name"] = df["last_place_name"].fillna("").astype(str)
    return df


def _econ(parser: DemoParser, rounds: pd.DataFrame) -> pd.DataFrame:
    """Per round, per player: side and equipment value a few seconds into the round."""
    wanted, tick_round = [], {}
    for r in rounds.itertuples(index=False):
        t = int(r.freeze_end_tick) + ECON_SAMPLE_OFFSET
        t = min(t, int(r.end_tick))
        wanted.append(t)
        tick_round[t] = int(r.round_idx)
    if not wanted:
        return pd.DataFrame()
    df = parser.parse_ticks(["current_equip_value", "team_num", "is_alive"], ticks=wanted)
    if df.empty:
        return df
    df = df.copy()
    df["round_idx"] = df["tick"].map(tick_round).astype("int64")
    return df


def _save(df: pd.DataFrame, path: Path) -> None:
    _str_ids(df).to_parquet(path, index=False)


def parse_demo(path: str | Path, force: bool = False) -> dict:
    """Parse a demo file into cached parquet artifacts keyed by SHA-256 of the file.

    Returns a dict with the hash, cache dir, and a summary of counts.
    """
    path = Path(path)
    demo_hash = hash_demo(path)
    out_dir = cache_dir(demo_hash)

    if not force and is_cached(demo_hash):
        return {"hash": demo_hash, "cache": out_dir, "cached": True}

    out_dir.mkdir(parents=True, exist_ok=True)
    parser = DemoParser(str(path))

    header = parser.parse_header()
    warmup_end = _warmup_end_tick(parser)
    players = parser.parse_player_info()
    bombs = _bombs(parser, warmup_end)
    rounds = _enrich_rounds(parser, _round_table(parser, warmup_end), bombs)
    deaths = _deaths(parser, warmup_end)
    damages = _damages(parser, warmup_end)
    shots = _shots(parser, warmup_end)
    util = _util(parser, warmup_end)
    blinds = _blinds(parser, warmup_end)
    ticks = _sampled_ticks(parser, rounds)
    econ = _econ(parser, rounds)

    header_out = {
        **header,
        "source_file": str(path.resolve()),
        "source_filename": path.name,
        "warmup_end_tick": warmup_end,
        "tickrate": TICKRATE,
        "cache_version": CACHE_VERSION,
    }
    (out_dir / "header.json").write_text(json.dumps(header_out, indent=2))
    _save(players, out_dir / "players.parquet")
    _save(rounds, out_dir / "rounds.parquet")
    _save(deaths, out_dir / "deaths.parquet")
    _save(bombs, out_dir / "bombs.parquet")
    _save(ticks, out_dir / "ticks.parquet")
    _save(damages, out_dir / "damages.parquet")
    _save(shots, out_dir / "shots.parquet")
    _save(util, out_dir / "util.parquet")
    _save(blinds, out_dir / "blinds.parquet")
    _save(econ, out_dir / "econ.parquet")

    return {
        "hash": demo_hash,
        "cache": out_dir,
        "cached": False,
        "map": header.get("map_name"),
        "server": header.get("server_name"),
        "players": len(players),
        "rounds": len(rounds),
        "deaths": len(deaths),
        "bombs": len(bombs),
    }

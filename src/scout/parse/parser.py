from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from demoparser2 import DemoParser

from .cache import CACHE_VERSION, cache_dir, is_cached
from .hashing import hash_demo

TICKRATE = 64  # CS2 demos are 64 ticks/s
TICK_SAMPLE_STEP = 32  # sample player positions every ~0.5 s
ECON_SAMPLE_OFFSET = 5 * TICKRATE  # equip value read 5 s into the round (after late buys)

TICK_PROPS = ["X", "Y", "Z", "last_place_name", "is_alive", "health", "team_num"]

# Every game event we need, parsed in ONE pass (parse_events) instead of one
# decode per event — demoparser2 re-reads the whole demo on each parse_event call,
# so this is the dominant speedup. Props are the UNION across events; events that
# lack a prop just get null columns we ignore (verified equal to per-event calls).
ALL_EVENTS = (
    "player_death", "player_hurt", "weapon_fire",
    "bomb_planted", "bomb_defused", "bomb_exploded",
    "smokegrenade_detonate", "flashbang_detonate", "hegrenade_detonate",
    "inferno_startburn", "decoy_detonate",
    "player_blind", "round_freeze_end", "round_end",
)
EVENT_PLAYER_PROPS = ["X", "Y", "Z", "team_num", "last_place_name"]
EVENT_OTHER_PROPS = ["total_rounds_played", "is_bomb_planted"]

UTIL_EVENTS = (
    "smokegrenade_detonate",
    "flashbang_detonate",
    "hegrenade_detonate",
    "inferno_startburn",
    "decoy_detonate",
)


def _parse_all_events(parser: DemoParser) -> dict[str, pd.DataFrame]:
    """All game events in a single decode pass. Returns {event_name: DataFrame};
    events with no occurrences are simply absent (use .get(name) → empty)."""
    combined = parser.parse_events(list(ALL_EVENTS),
                                   player=EVENT_PLAYER_PROPS, other=EVENT_OTHER_PROPS)
    out: dict[str, pd.DataFrame] = {}
    for item in combined or []:
        try:
            name, df = item
        except (TypeError, ValueError):
            continue
        if isinstance(df, pd.DataFrame) and not df.empty:
            out[name] = df
    return out


def _ev(events: dict[str, pd.DataFrame], name: str) -> pd.DataFrame:
    return events.get(name, pd.DataFrame())


def _str_ids(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize every *steamid* column to plain strings so joins work across tables."""
    for col in df.columns:
        if "steamid" in col:
            df[col] = df[col].astype("string").astype(str)
    return df


def _warmup_and_rounds(parser: DemoParser) -> tuple[int, pd.DataFrame]:
    """ONE full tick scan → (warmup_end_tick, round table). Combines what used to be
    two separate full-demo tick scans."""
    ticks = parser.parse_ticks(["is_warmup_period", "total_rounds_played"])
    live_all = ticks.loc[~ticks["is_warmup_period"], "tick"]
    warmup_end = int(live_all.iloc[0]) if len(live_all) else 0

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
    return warmup_end, first_ticks[["round_idx", "start_tick", "end_tick"]]


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


def _enrich_rounds(events: dict, rounds: pd.DataFrame, bombs: pd.DataFrame) -> pd.DataFrame:
    """Attach freeze_end_tick, winner/reason, and bomb plant info to each round."""
    freeze = _ev(events, "round_freeze_end")
    freeze_ticks = freeze["tick"].sort_values() if "tick" in freeze else pd.Series(dtype="int64")
    ends = _ev(events, "round_end")

    plants = bombs[bombs["event"] == "bomb_planted"] if len(bombs) else pd.DataFrame()

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


def _after_warmup(df: pd.DataFrame, warmup_end: int) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["tick"] >= warmup_end].reset_index(drop=True)


def _deaths(events: dict, warmup_end: int) -> pd.DataFrame:
    return _after_warmup(_ev(events, "player_death"), warmup_end)


def _damages(events: dict, warmup_end: int) -> pd.DataFrame:
    return _after_warmup(_ev(events, "player_hurt"), warmup_end)


def _shots(events: dict, warmup_end: int) -> pd.DataFrame:
    return _after_warmup(_ev(events, "weapon_fire"), warmup_end)


def _blinds(events: dict, warmup_end: int) -> pd.DataFrame:
    return _after_warmup(_ev(events, "player_blind"), warmup_end)


def _bombs(events: dict, warmup_end: int) -> pd.DataFrame:
    frames = []
    for event in ("bomb_planted", "bomb_defused", "bomb_exploded"):
        df = _ev(events, event)
        if df.empty:
            continue
        df = df.copy()
        df["event"] = event
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    return _after_warmup(merged, warmup_end)


def _util(events: dict, warmup_end: int) -> pd.DataFrame:
    frames = []
    for event in UTIL_EVENTS:
        df = _ev(events, event)
        if df.empty:
            continue
        df = df.copy()
        df["event"] = event
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True, sort=False)
    merged = _after_warmup(merged, warmup_end)
    renames = {
        "user_steamid": "thrower_steamid",
        "user_name": "thrower_name",
        "user_team_num": "thrower_team_num",
        "user_X": "thrower_X",
        "user_Y": "thrower_Y",
    }
    return merged.rename(columns={k: v for k, v in renames.items() if k in merged.columns})


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
    players = parser.parse_player_info()
    events = _parse_all_events(parser)          # one decode pass for all events
    warmup_end, round_table = _warmup_and_rounds(parser)  # one full tick scan
    bombs = _bombs(events, warmup_end)
    rounds = _enrich_rounds(events, round_table, bombs)
    deaths = _deaths(events, warmup_end)
    damages = _damages(events, warmup_end)
    shots = _shots(events, warmup_end)
    util = _util(events, warmup_end)
    blinds = _blinds(events, warmup_end)
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

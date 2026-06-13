from __future__ import annotations

import pandas as pd

from .loader import SIDE_NAME, MatchSet

TICKRATE = 64
SAMPLE_SECONDS = 0.5  # parser samples every 32 ticks

PHASES = ("all", "opening (0-20s)", "mid round", "post-plant")


def position_samples(
    ms: MatchSet,
    steamid: str,
    side: str | None = None,
    phase: str = "all",
    alive_only: bool = True,
) -> pd.DataFrame:
    """Sampled positions for one player, filtered by side and round phase."""
    if ms.ticks.empty:
        return pd.DataFrame()
    t = ms.ticks[ms.ticks["steamid"].astype(str) == str(steamid)].copy()
    if t.empty:
        return t
    if alive_only and "is_alive" in t.columns:
        t = t[t["is_alive"].astype(bool)]
    t["side"] = t["team_num"].map(SIDE_NAME)
    if side in ("T", "CT"):
        t = t[t["side"] == side]
    if phase != "all" and not ms.rounds.empty:
        plant = ms.rounds[["demo_hash", "round_idx", "plant_tick"]]
        t = t.merge(plant, on=["demo_hash", "round_idx"], how="left")
        planted = t["plant_tick"].notna() & (t["tick"] >= t["plant_tick"])
        if phase.startswith("opening"):
            t = t[t["secs"] <= 20]
        elif phase == "mid round":
            t = t[(t["secs"] > 20) & ~planted]
        elif phase == "post-plant":
            t = t[planted]
    return t.reset_index(drop=True)


def place_time(samples: pd.DataFrame, top: int = 12) -> pd.DataFrame:
    """Seconds spent per named map area, with share of total."""
    if samples.empty:
        return pd.DataFrame()
    s = samples[samples["last_place_name"] != ""]
    if s.empty:
        return pd.DataFrame()
    g = s.groupby("last_place_name").size().mul(SAMPLE_SECONDS).rename("seconds")
    g = g.sort_values(ascending=False).head(top).reset_index()
    g = g.rename(columns={"last_place_name": "area"})
    g["share %"] = (g["seconds"] / g["seconds"].sum() * 100).round(1)
    g["seconds"] = g["seconds"].round(0).astype(int)
    return g


def place_at(samples: pd.DataFrame, second: float, tol: float = 2.0) -> pd.DataFrame:
    """Where the player stands at a given second of the round, across all rounds."""
    if samples.empty:
        return pd.DataFrame()
    s = samples[(samples["secs"] >= second - tol) & (samples["secs"] <= second + tol)]
    s = s[s["last_place_name"] != ""]
    if s.empty:
        return pd.DataFrame()
    per_round = (
        s.groupby(["demo_hash", "round_idx"])["last_place_name"]
        .agg(lambda x: x.mode().iloc[0])
        .reset_index()
    )
    counts = per_round["last_place_name"].value_counts().reset_index()
    counts.columns = ["area", "rounds"]
    counts["share %"] = (counts["rounds"] / counts["rounds"].sum() * 100).round(1)
    return counts


def transitions(samples: pd.DataFrame, min_run_samples: int = 2, top: int = 15) -> pd.DataFrame:
    """Most common area-to-area movements (rotations) with typical timing.

    Collapses consecutive samples in the same area into runs; a transition is a
    switch between two runs that each lasted at least min_run_samples (~1 s).
    """
    if samples.empty:
        return pd.DataFrame()
    rows = []
    s = samples[samples["last_place_name"] != ""].sort_values(["demo_hash", "round_idx", "tick"])
    for (_, _), grp in s.groupby(["demo_hash", "round_idx"]):
        places = grp["last_place_name"].to_numpy()
        secs = grp["secs"].to_numpy()
        runs: list[tuple[str, int, float]] = []  # (place, n_samples, first_sec)
        for p, sec in zip(places, secs):
            if runs and runs[-1][0] == p:
                runs[-1] = (p, runs[-1][1] + 1, runs[-1][2])
            else:
                runs.append((p, 1, float(sec)))
        solid = [r for r in runs if r[1] >= min_run_samples]
        for a, b in zip(solid, solid[1:]):
            rows.append({"from": a[0], "to": b[0], "sec": b[2]})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    g = (
        df.groupby(["from", "to"])
        .agg(times=("sec", "size"), median_sec=("sec", "median"))
        .reset_index()
        .sort_values("times", ascending=False)
        .head(top)
    )
    g["median_sec"] = g["median_sec"].round(0).astype(int)
    return g.rename(columns={"median_sec": "typical time (s)"})


def list_rounds(ms: MatchSet, steamid: str, side: str | None = None) -> pd.DataFrame:
    """Rounds the player was sampled in, for the movement-map round picker."""
    if ms.ticks.empty:
        return pd.DataFrame()
    t = ms.ticks[ms.ticks["steamid"].astype(str) == str(steamid)].copy()
    if t.empty:
        return t
    t["side"] = t["team_num"].map(SIDE_NAME)
    if side in ("T", "CT"):
        t = t[t["side"] == side]
    g = t.groupby(["demo_hash", "demo_label", "map_name", "round_idx"], as_index=False)["side"].first()
    if not ms.rounds.empty:
        g = g.merge(
            ms.rounds[["demo_hash", "round_idx", "winner", "plant_site"]],
            on=["demo_hash", "round_idx"],
            how="left",
        )
        g["won"] = g["winner"].astype(str) == g["side"]
    return g.sort_values(["demo_label", "round_idx"]).reset_index(drop=True)


def round_path(ms: MatchSet, steamid: str, demo_hash: str, round_idx: int) -> pd.DataFrame:
    """Ordered alive positions of one player in one round (for trajectory drawing)."""
    if ms.ticks.empty:
        return pd.DataFrame()
    t = ms.ticks
    path = t[
        (t["steamid"].astype(str) == str(steamid))
        & (t["demo_hash"] == demo_hash)
        & (t["round_idx"] == round_idx)
    ]
    if "is_alive" in path.columns:
        path = path[path["is_alive"].astype(bool)]
    return path.sort_values("tick").reset_index(drop=True)

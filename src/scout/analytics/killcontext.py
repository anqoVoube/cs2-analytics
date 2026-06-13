from __future__ import annotations

import numpy as np
import pandas as pd

from .loader import SIDE_NAME, MatchSet
from .stats import TICKRATE

HOLD_MOVE_UNITS = 200.0  # moved less than this in the 5 s before a kill = holding an angle
PRE_KILL_SECS = 5
CAME_FROM_SECS = 10
FLASH_WINDOW_SECS = 4
FLASH_RADIUS = 800.0
SMOKE_WINDOW_SECS = 18  # smoke lifetime
SMOKE_RADIUS = 350.0


def kill_log(ms: MatchSet, steamid: str) -> pd.DataFrame:
    """Every kill by the player with timing, location, and pre-kill context.

    Context per kill: seconds into the round, attacker/victim areas, where the
    killer was 10 s earlier, whether he was holding still or pushing, whether his
    team flashed just before, and whether the fight happened around a smoke.
    """
    steamid = str(steamid)
    if ms.deaths.empty or ms.rounds.empty:
        return pd.DataFrame()
    k = ms.deaths[
        (ms.deaths["attacker_steamid"].astype(str) == steamid)
        & (ms.deaths["user_steamid"].astype(str) != steamid)
    ].copy()
    if k.empty:
        return pd.DataFrame()
    k = k.merge(
        ms.rounds[["demo_hash", "round_idx", "freeze_end_tick"]],
        on=["demo_hash", "round_idx"],
        how="inner",
    )

    t = ms.ticks[ms.ticks["steamid"].astype(str) == steamid] if not ms.ticks.empty else pd.DataFrame()
    track = {key: g.sort_values("tick") for key, g in t.groupby(["demo_hash", "round_idx"])} if len(t) else {}
    util = ms.util if not ms.util.empty else pd.DataFrame()

    rows = []
    for r in k.itertuples():
        moved, came_from = np.nan, ""
        g = track.get((r.demo_hash, r.round_idx))
        if g is not None and len(g) > 1:
            w = g[(g["tick"] >= r.tick - PRE_KILL_SECS * TICKRATE) & (g["tick"] <= r.tick)]
            if len(w) >= 2:
                moved = float(np.hypot(np.diff(w["X"].to_numpy()), np.diff(w["Y"].to_numpy())).sum())
            lo = r.tick - (CAME_FROM_SECS + 2) * TICKRATE
            hi = r.tick - (CAME_FROM_SECS - 2) * TICKRATE
            before = g[(g["tick"] >= lo) & (g["tick"] <= hi)]
            places = before["last_place_name"][before["last_place_name"] != ""]
            if len(places):
                came_from = str(places.mode().iloc[0])

        flash_before = smoke_near = False
        vic_x, vic_y = getattr(r, "user_X", np.nan), getattr(r, "user_Y", np.nan)
        if len(util) and pd.notna(vic_x):
            u = util[(util["demo_hash"] == r.demo_hash) & (util["round_idx"] == r.round_idx)]
            fl = u[
                (u["event"] == "flashbang_detonate")
                & (u["thrower_team_num"] == r.attacker_team_num)
                & (u["tick"] >= r.tick - FLASH_WINDOW_SECS * TICKRATE)
                & (u["tick"] <= r.tick)
            ]
            if len(fl):
                flash_before = bool(
                    (np.hypot(fl["x"] - vic_x, fl["y"] - vic_y) <= FLASH_RADIUS).any()
                )
            mid_x = (getattr(r, "attacker_X", vic_x) + vic_x) / 2
            mid_y = (getattr(r, "attacker_Y", vic_y) + vic_y) / 2
            sm = u[
                (u["event"] == "smokegrenade_detonate")
                & (u["tick"] >= r.tick - SMOKE_WINDOW_SECS * TICKRATE)
                & (u["tick"] <= r.tick)
            ]
            if len(sm):
                smoke_near = bool(
                    (np.hypot(sm["x"] - mid_x, sm["y"] - mid_y) <= SMOKE_RADIUS).any()
                )

        thrusmoke = bool(getattr(r, "thrusmoke", False))
        behavior = "" if pd.isna(moved) else ("holding" if moved < HOLD_MOVE_UNITS else "moving")
        rows.append(
            {
                "demo": r.demo_label,
                "map": r.map_name,
                "round": int(r.round_idx) + 1,
                "side": SIDE_NAME.get(getattr(r, "attacker_team_num", None), "?"),
                "sec": round((r.tick - r.freeze_end_tick) / TICKRATE, 1),
                "place": str(getattr(r, "attacker_last_place_name", "") or ""),
                "victim place": str(getattr(r, "user_last_place_name", "") or ""),
                "came from": came_from,
                "behavior": behavior,
                "weapon": str(r.weapon),
                "off team flash": flash_before,
                "around smoke": smoke_near or thrusmoke,
                "through smoke": thrusmoke,
                "wallbang": bool(getattr(r, "penetrated", 0)),
                "noscope": bool(getattr(r, "noscope", False)),
                "while blind": bool(getattr(r, "attackerblind", False)),
                "headshot": bool(getattr(r, "headshot", False)),
            }
        )
    return pd.DataFrame(rows).sort_values(["demo", "round", "sec"]).reset_index(drop=True)


def kill_summary(log: pd.DataFrame, top: int = 8) -> dict:
    """Aggregates over a kill log: where & when they kill, and how."""
    if log.empty:
        return {}
    known = log[log["place"] != ""]
    where = (
        known.groupby("place")
        .agg(
            kills=("sec", "size"),
            median_sec=("sec", "median"),
            holding=("behavior", lambda b: (b == "holding").mean() * 100),
        )
        .sort_values("kills", ascending=False)
        .head(top)
        .reset_index()
    )
    where["median_sec"] = where["median_sec"].round(0).astype(int)
    where["holding"] = where["holding"].round(0).astype(int)
    where = where.rename(columns={"median_sec": "typical sec", "holding": "holding %"})

    behav = log[log["behavior"] != ""]
    routes = log[(log["came from"] != "") & (log["came from"] != log["place"])]
    route_counts = (
        routes.groupby(["came from", "place"]).size().rename("kills")
        .sort_values(ascending=False).head(5).reset_index()
    )
    return {
        "where_when": where,
        "routes": route_counts,
        "n": len(log),
        "holding_pct": float((behav["behavior"] == "holding").mean() * 100) if len(behav) else None,
        "flash_pct": float(log["off team flash"].mean() * 100),
        "smoke_pct": float(log["around smoke"].mean() * 100),
        "through_smoke": int(log["through smoke"].sum()),
        "wallbangs": int(log["wallbang"].sum()),
        "noscopes": int(log["noscope"].sum()),
    }

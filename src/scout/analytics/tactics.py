from __future__ import annotations

import pandas as pd

from .loader import CT_SIDE, T_SIDE, MatchSet
from .stats import team_membership

TICKRATE = 64
FAST_PLANT_SEC = 45  # plant before this = "fast" execute
PISTOL_ROUNDS = (0, 12)  # MR12 first round of each half (0-based)


def buy_label(avg_equip: float, round_idx: int) -> str:
    if round_idx in PISTOL_ROUNDS:
        return "pistol"
    if avg_equip < 2000:
        return "eco"
    if avg_equip < 3900:
        return "force"
    return "full buy"


def round_book(ms: MatchSet, steamid: str, side: str = "T") -> pd.DataFrame:
    """One row per round the selected player's team played on `side`.

    Captures buy type, plant site and timing, first contact, the team's
    map spread at 20 s, and the result — the raw material for tactic patterns.
    """
    membership = team_membership(ms, steamid)
    if membership.empty or ms.rounds.empty:
        return pd.DataFrame()
    side_num = T_SIDE if side == "T" else CT_SIDE

    team = membership[membership["is_teammate"]]
    on_side = team[team["team_num"] == side_num]
    if on_side.empty:
        return pd.DataFrame()

    deaths = ms.deaths.sort_values("tick") if not ms.deaths.empty else pd.DataFrame()
    ticks = ms.ticks

    rows = []
    for (demo_hash, round_idx), grp in on_side.groupby(["demo_hash", "round_idx"]):
        mates = set(grp["steamid"].astype(str))
        rinfo = ms.rounds[
            (ms.rounds["demo_hash"] == demo_hash) & (ms.rounds["round_idx"] == round_idx)
        ]
        if rinfo.empty:
            continue
        rinfo = rinfo.iloc[0]
        freeze = rinfo["freeze_end_tick"]

        equip = ms.econ[
            (ms.econ["demo_hash"] == demo_hash)
            & (ms.econ["round_idx"] == round_idx)
            & (ms.econ["steamid"].astype(str).isin(mates))
        ]["current_equip_value"].mean()

        plant_sec = plant_site = None
        if pd.notna(rinfo["plant_tick"]):
            plant_sec = round((int(rinfo["plant_tick"]) - freeze) / TICKRATE, 1)
            plant_site = rinfo["plant_site"]

        first_sec = first_role = first_place = None
        if not deaths.empty:
            rd = deaths[(deaths["demo_hash"] == demo_hash) & (deaths["round_idx"] == round_idx)]
            if len(rd):
                first = rd.iloc[0]
                first_sec = round((int(first["tick"]) - freeze) / TICKRATE, 1)
                killer_on_team = str(first["attacker_steamid"]) in mates
                victim_on_team = str(first["user_steamid"]) in mates
                if killer_on_team and not victim_on_team:
                    first_role = "entry kill"
                elif victim_on_team:
                    first_role = "entry death"
                else:
                    first_role = "other"
                first_place = str(first.get("user_last_place_name") or "")

        spread = ""
        if not ticks.empty:
            snap = ticks[
                (ticks["demo_hash"] == demo_hash)
                & (ticks["round_idx"] == round_idx)
                & (ticks["steamid"].astype(str).isin(mates))
                & (ticks["secs"] >= 18)
                & (ticks["secs"] <= 22)
                & (ticks["last_place_name"] != "")
            ]
            if len(snap):
                per_player = snap.groupby("steamid")["last_place_name"].agg(
                    lambda x: x.mode().iloc[0]
                )
                counts = per_player.value_counts()
                spread = ", ".join(f"{n} {place}" for place, n in counts.items())

        if plant_site is not None:
            tactic = f"{'Fast' if plant_sec is not None and plant_sec < FAST_PLANT_SEC else 'Slow'} {plant_site}"
        else:
            tactic = "No plant"

        won = str(rinfo["winner"]) == side
        rows.append(
            {
                "demo": rinfo.get("demo_label", ""),
                "map": rinfo.get("map_name", ""),
                "round": int(round_idx) + 1,
                "buy": buy_label(float(equip) if pd.notna(equip) else 0.0, int(round_idx)),
                "tactic": tactic,
                "plant site": plant_site or "—",
                "plant sec": plant_sec,
                "first contact": first_role or "—",
                "contact sec": first_sec,
                "contact place": first_place or "—",
                "spread @20s": spread,
                "result": "WIN" if won else "loss",
                "demo_hash": demo_hash,
                "round_idx": int(round_idx),
            }
        )
    book = pd.DataFrame(rows)
    if len(book):
        book = book.sort_values(["demo", "round"]).reset_index(drop=True)
    return book


def tactic_summary(book: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Aggregate tendencies from a round book: what they run and what wins."""
    if book.empty:
        return {}
    out: dict[str, pd.DataFrame] = {}

    def rate(group_col: str) -> pd.DataFrame:
        g = book.groupby(group_col).agg(
            rounds=("result", "size"),
            wins=("result", lambda s: (s == "WIN").sum()),
        )
        g["win %"] = (g["wins"] / g["rounds"] * 100).round(0).astype(int)
        g["share %"] = (g["rounds"] / len(book) * 100).round(0).astype(int)
        return g.sort_values("rounds", ascending=False).reset_index()

    out["by_tactic"] = rate("tactic")
    out["by_buy"] = rate("buy")

    planted = book[book["plant site"] != "—"]
    if len(planted):
        out["by_site"] = (
            planted.groupby("plant site")
            .agg(
                plants=("result", "size"),
                wins=("result", lambda s: (s == "WIN").sum()),
                avg_plant_sec=("plant sec", "mean"),
            )
            .assign(**{"win %": lambda d: (d["wins"] / d["plants"] * 100).round(0).astype(int)})
            .reset_index()
        )
        out["by_site"]["avg_plant_sec"] = out["by_site"]["avg_plant_sec"].round(0).astype(int)

    contact = book[book["contact place"] != "—"]
    if len(contact):
        out["contact_places"] = (
            contact["contact place"].value_counts().head(10).reset_index()
        )
        out["contact_places"].columns = ["first contact area", "rounds"]
    return out

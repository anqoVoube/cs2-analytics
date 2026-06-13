from __future__ import annotations

import numpy as np
import pandas as pd

from .loader import CT_SIDE, SIDE_NAME, T_SIDE, MatchSet

TICKRATE = 64
TRADE_WINDOW_TICKS = 5 * TICKRATE  # a death answered within 5 s counts as traded

UTILITY_WEAPONS = {"hegrenade", "molotov", "incgrenade", "inferno", "flashbang",
                   "smokegrenade", "decoy", "world", "planted_c4"}
NON_GUN = UTILITY_WEAPONS | {"knife", "knife_t", "bayonet", "taser"}


def _norm_weapon(weapon: pd.Series) -> pd.Series:
    """Strip the 'weapon_' prefix weapon_fire events carry but player_hurt events don't."""
    return weapon.fillna("").astype(str).str.lower().str.removeprefix("weapon_")


def _is_gun(weapon: pd.Series) -> pd.Series:
    w = _norm_weapon(weapon)
    return (w != "") & ~w.isin(NON_GUN) & ~w.str.startswith("knife")


def player_rounds(ms: MatchSet, steamid: str) -> pd.DataFrame:
    """One row per round the player appears in: side, equip value, round outcome."""
    if ms.econ.empty or ms.rounds.empty:
        return pd.DataFrame()
    mine = ms.econ[ms.econ["steamid"] == str(steamid)].copy()
    if mine.empty:
        return mine
    rounds = ms.rounds[
        ["demo_hash", "round_idx", "freeze_end_tick", "end_tick", "winner",
         "reason", "plant_tick", "plant_site", "map_name", "demo_label"]
    ]
    # inner join: econ rows from aborted/post-match rounds have no rounds entry
    out = mine[["demo_hash", "round_idx", "team_num", "current_equip_value"]].merge(
        rounds, on=["demo_hash", "round_idx"], how="inner"
    )
    out["side"] = out["team_num"].map(SIDE_NAME)
    out["won"] = out["winner"].astype(str) == out["side"]
    return out


def team_membership(ms: MatchSet, steamid: str) -> pd.DataFrame:
    """Per (demo, round): steamids on the selected player's team and on the opposing team."""
    if ms.econ.empty:
        return pd.DataFrame()
    e = ms.econ[["demo_hash", "round_idx", "steamid", "name", "team_num"]].copy()
    mine = e[e["steamid"] == str(steamid)][["demo_hash", "round_idx", "team_num"]]
    mine = mine.rename(columns={"team_num": "my_team"})
    merged = e.merge(mine, on=["demo_hash", "round_idx"], how="inner")
    merged["is_teammate"] = merged["team_num"] == merged["my_team"]
    return merged


def _deaths_sorted(ms: MatchSet) -> pd.DataFrame:
    if ms.deaths.empty:
        return pd.DataFrame()
    d = ms.deaths.copy()
    d = d.sort_values(["demo_hash", "round_idx", "tick"]).reset_index(drop=True)
    return d


def round_ledger(ms: MatchSet, steamid: str) -> pd.DataFrame:
    """Per-round performance ledger for one player.

    Columns: kills, deaths, assists, damage, survived, traded, opening
    ('kill'/'death'/None), clutch ('1v3 won' style), kast, side, won, equip.
    """
    steamid = str(steamid)
    rounds = player_rounds(ms, steamid)
    if rounds.empty:
        return rounds
    deaths = _deaths_sorted(ms)
    membership = team_membership(ms, steamid)
    damages = ms.damages

    ledger_rows = []
    for r in rounds.itertuples(index=False):
        key = (r.demo_hash, r.round_idx)
        rd = deaths[(deaths["demo_hash"] == key[0]) & (deaths["round_idx"] == key[1])] \
            if not deaths.empty else pd.DataFrame()
        team = membership[
            (membership["demo_hash"] == key[0]) & (membership["round_idx"] == key[1])
        ] if not membership.empty else pd.DataFrame()
        teammates = set(team.loc[team["is_teammate"], "steamid"]) - {steamid} if len(team) else set()
        opponents = set(team.loc[~team["is_teammate"], "steamid"]) if len(team) else set()

        kills = deaths_n = assists = 0
        survived, traded, opening, clutch = True, False, None, None
        my_death_tick = None

        if len(rd):
            att = rd["attacker_steamid"].astype(str)
            vic = rd["user_steamid"].astype(str)
            ast = rd["assister_steamid"].astype(str) if "assister_steamid" in rd else pd.Series(dtype=str)
            kills = int(((att == steamid) & (vic != steamid)).sum())
            assists = int((ast == steamid).sum()) if len(ast) else 0
            my_deaths = rd[vic == steamid]
            deaths_n = len(my_deaths)
            survived = deaths_n == 0
            if deaths_n:
                first = my_deaths.iloc[0]
                my_death_tick = int(first["tick"])
                killer = str(first["attacker_steamid"])
                after = rd[(rd["tick"] > my_death_tick)
                           & (rd["tick"] <= my_death_tick + TRADE_WINDOW_TICKS)]
                traded = bool((after["user_steamid"].astype(str) == killer).any())

            first_death = rd.iloc[0]
            if str(first_death["attacker_steamid"]) == steamid:
                opening = "kill"
            elif str(first_death["user_steamid"]) == steamid:
                opening = "death"

            # Clutch: last living teammate dies while the player is still alive.
            if teammates and opponents:
                mate_deaths = rd[vic.isin(teammates)]
                if mate_deaths["user_steamid"].nunique() == len(teammates):
                    t_last = int(mate_deaths["tick"].max())
                    alive_then = my_death_tick is None or my_death_tick > t_last
                    if alive_then:
                        opp_dead = rd[(vic.isin(opponents)) & (rd["tick"] <= t_last)]
                        n_opp = len(opponents) - opp_dead["user_steamid"].nunique()
                        if n_opp >= 1:
                            clutch = f"1v{n_opp} {'won' if r.won else 'lost'}"

        damage = 0.0
        if not damages.empty:
            dd = damages[
                (damages["demo_hash"] == key[0])
                & (damages["round_idx"] == key[1])
                & (damages["attacker_steamid"].astype(str) == steamid)
                & (damages["user_steamid"].astype(str) != steamid)
            ]
            if "attacker_team_num" in dd.columns and len(dd):
                dd = dd[dd["attacker_team_num"] != dd["user_team_num"]]
            damage = float(np.minimum(dd["dmg_health"].to_numpy(dtype=float), 100).sum()) if len(dd) else 0.0

        kast = bool(kills or assists or survived or traded)
        ledger_rows.append(
            {
                "demo_hash": r.demo_hash,
                "demo_label": r.demo_label,
                "map_name": r.map_name,
                "round_idx": r.round_idx,
                "side": r.side,
                "won": bool(r.won),
                "equip": r.current_equip_value,
                "kills": kills,
                "deaths": deaths_n,
                "assists": assists,
                "damage": damage,
                "survived": survived,
                "traded": traded,
                "opening": opening,
                "clutch": clutch,
                "kast": kast,
            }
        )
    return pd.DataFrame(ledger_rows)


def overview(ms: MatchSet, steamid: str, ledger: pd.DataFrame | None = None) -> dict:
    """Headline stats for one player across the (already filtered) MatchSet."""
    steamid = str(steamid)
    if ledger is None:
        ledger = round_ledger(ms, steamid)
    if ledger.empty:
        return {}
    n = len(ledger)
    kills, deaths = int(ledger["kills"].sum()), int(ledger["deaths"].sum())

    hs_pct = 0.0
    if not ms.deaths.empty:
        my_kills = ms.deaths[ms.deaths["attacker_steamid"].astype(str) == steamid]
        gun_kills = my_kills[_is_gun(my_kills["weapon"])]
        if len(gun_kills):
            hs_pct = float(gun_kills["headshot"].astype(bool).mean() * 100)

    openings = ledger["opening"].value_counts()
    open_k, open_d = int(openings.get("kill", 0)), int(openings.get("death", 0))
    open_won = int(ledger.loc[(ledger["opening"] == "kill") & ledger["won"], "opening"].count())

    clutches = ledger["clutch"].dropna()
    clutch_won = int(clutches.str.contains("won").sum())

    multi = ledger["kills"].value_counts()
    aces = int(ledger.loc[ledger["kills"] >= 5, "kills"].count())

    return {
        "rounds": n,
        "matches": int(ledger["demo_hash"].nunique()),
        "maps": int(ledger["map_name"].nunique()),
        "kills": kills,
        "deaths": deaths,
        "assists": int(ledger["assists"].sum()),
        "kd": kills / max(deaths, 1),
        "kpr": kills / n,
        "adr": float(ledger["damage"].sum()) / n,
        "kast": float(ledger["kast"].mean() * 100),
        "hs_pct": hs_pct,
        "win_pct": float(ledger["won"].mean() * 100),
        "opening_kills": open_k,
        "opening_deaths": open_d,
        "opening_win_pct": (open_won / open_k * 100) if open_k else 0.0,
        "clutch_attempts": int(len(clutches)),
        "clutch_won": clutch_won,
        "traded_pct": float(ledger.loc[ledger["deaths"] > 0, "traded"].mean() * 100)
        if (ledger["deaths"] > 0).any() else 0.0,
        "2k": int(multi.get(2, 0)),
        "3k": int(multi.get(3, 0)),
        "4k": int(multi.get(4, 0)),
        "5k": aces,
    }


def side_split(ledger: pd.DataFrame) -> pd.DataFrame:
    """T vs CT split of the round ledger."""
    if ledger.empty:
        return pd.DataFrame()
    rows = []
    for side, grp in ledger.groupby("side"):
        n = len(grp)
        rows.append(
            {
                "Side": side,
                "Rounds": n,
                "Kills": int(grp["kills"].sum()),
                "Deaths": int(grp["deaths"].sum()),
                "K/D": round(grp["kills"].sum() / max(grp["deaths"].sum(), 1), 2),
                "ADR": round(grp["damage"].sum() / n, 1),
                "KAST %": round(grp["kast"].mean() * 100, 1),
                "Round win %": round(grp["won"].mean() * 100, 1),
            }
        )
    return pd.DataFrame(rows)


def opening_detail(ms: MatchSet, steamid: str) -> pd.DataFrame:
    """Every opening duel involving the player: where, with what, and did the round get won."""
    steamid = str(steamid)
    deaths = _deaths_sorted(ms)
    if deaths.empty:
        return pd.DataFrame()
    firsts = deaths.groupby(["demo_hash", "round_idx"], as_index=False).first()
    mine = firsts[
        (firsts["attacker_steamid"].astype(str) == steamid)
        | (firsts["user_steamid"].astype(str) == steamid)
    ].copy()
    if mine.empty:
        return mine
    mine["role"] = np.where(mine["attacker_steamid"].astype(str) == steamid, "kill", "death")
    mine["place"] = mine["user_last_place_name"].fillna("")
    rounds = ms.rounds[["demo_hash", "round_idx", "freeze_end_tick", "winner"]]
    mine = mine.merge(rounds, on=["demo_hash", "round_idx"], how="left")
    mine["sec"] = ((mine["tick"] - mine["freeze_end_tick"]) / TICKRATE).round(1)
    side = np.where(mine["role"] == "kill", mine.get("attacker_team_num"), mine.get("user_team_num"))
    mine["side"] = pd.Series(side, index=mine.index).map(SIDE_NAME)
    return mine[["demo_label", "map_name", "round_idx", "side", "role", "sec",
                 "place", "weapon"]].sort_values(["demo_label", "round_idx"])


def weapon_tables(ms: MatchSet, steamid: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(kills by weapon, deaths by enemy weapon) for the player."""
    steamid = str(steamid)
    if ms.deaths.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = ms.deaths
    kills = d[(d["attacker_steamid"].astype(str) == steamid)
              & (d["user_steamid"].astype(str) != steamid)]
    deaths = d[d["user_steamid"].astype(str) == steamid]

    def table(df: pd.DataFrame, hs: bool) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        g = df.groupby("weapon").agg(count=("weapon", "size"), hs=("headshot", "sum"))
        g = g.sort_values("count", ascending=False).reset_index()
        if hs:
            g["hs %"] = (g["hs"] / g["count"] * 100).round(0).astype(int)
        return g.drop(columns=["hs"])

    return table(kills, hs=True), table(deaths, hs=False)


def accuracy(ms: MatchSet, steamid: str) -> dict:
    """Bullets fired vs hits landed vs headshot hits."""
    steamid = str(steamid)
    if ms.shots.empty:
        return {}
    s = ms.shots[ms.shots["user_steamid"].astype(str) == steamid]
    s = s[_is_gun(s["weapon"])]
    fired = len(s)
    if not fired:
        return {}
    hits = head = 0
    if not ms.damages.empty:
        h = ms.damages[ms.damages["attacker_steamid"].astype(str) == steamid]
        h = h[_is_gun(h["weapon"])]
        hits = len(h)
        if "hitgroup" in h.columns and len(h):
            head = int(h["hitgroup"].astype(str).str.contains("head", case=False).sum())
    return {
        "shots": fired,
        "hits": hits,
        "accuracy_pct": hits / fired * 100,
        "head_hit_pct": (head / hits * 100) if hits else 0.0,
    }


FIRST_BULLET_GAP_TICKS = 64  # no shot in the previous second => crosshair-placement shot
HIT_MATCH_TICKS = 8  # hitscan damage registers within a few ticks of the shot


def first_bullet_accuracy(ms: MatchSet, steamid: str) -> dict:
    """Accuracy of 'first bullets' — shots fired after at least 1 s of not shooting.

    These are the shots where crosshair placement and flick aim matter most,
    as opposed to spray bullets. A shot counts as a hit if a gun-damage event
    by the same player lands within HIT_MATCH_TICKS after it.
    """
    steamid = str(steamid)
    if ms.shots.empty:
        return {}
    s = ms.shots[ms.shots["user_steamid"].astype(str) == steamid].copy()
    s["weapon"] = _norm_weapon(s["weapon"])
    s = s[_is_gun(s["weapon"])]
    if s.empty:
        return {}
    s = s.sort_values(["demo_hash", "round_idx", "tick"])
    prev_tick = s.groupby(["demo_hash", "round_idx"])["tick"].shift(1)
    first = s[prev_tick.isna() | (s["tick"] - prev_tick > FIRST_BULLET_GAP_TICKS)].copy()
    if first.empty:
        return {}

    if ms.damages.empty:
        merged = first.assign(hit_tick=pd.NA, hitgroup="")
    else:
        h = ms.damages[ms.damages["attacker_steamid"].astype(str) == steamid].copy()
        h["weapon"] = _norm_weapon(h["weapon"])
        h = h[_is_gun(h["weapon"])]
        h = h[["demo_hash", "round_idx", "tick", "hitgroup"]].rename(columns={"tick": "hit_tick"})
        merged = pd.merge_asof(
            first.sort_values("tick"),
            h.sort_values("hit_tick"),
            left_on="tick",
            right_on="hit_tick",
            by=["demo_hash", "round_idx"],
            direction="forward",
            tolerance=HIT_MATCH_TICKS,
        )

    merged["hit"] = merged["hit_tick"].notna()
    merged["head"] = merged["hit"] & merged["hitgroup"].astype(str).str.contains("head", case=False)
    n, hits, heads = len(merged), int(merged["hit"].sum()), int(merged["head"].sum())

    by_weapon = (
        merged.groupby("weapon")
        .agg(**{"first bullets": ("hit", "size"), "_hits": ("hit", "sum"), "_heads": ("head", "sum")})
        .reset_index()
        .sort_values("first bullets", ascending=False)
    )
    by_weapon["hit %"] = (by_weapon["_hits"] / by_weapon["first bullets"] * 100).round(0).astype(int)
    by_weapon["headshot %"] = (by_weapon["_heads"] / by_weapon["first bullets"] * 100).round(0).astype(int)
    by_weapon = by_weapon.drop(columns=["_hits", "_heads"])

    return {
        "first_bullets": n,
        "hit_pct": hits / n * 100,
        "head_pct": heads / n * 100,
        "by_weapon": by_weapon,
    }


def kill_timing(ms: MatchSet, steamid: str) -> dict:
    """When in the round the player gets kills and dies (seconds after freeze end)."""
    steamid = str(steamid)
    if ms.deaths.empty or ms.rounds.empty:
        return {}
    d = ms.deaths.merge(
        ms.rounds[["demo_hash", "round_idx", "freeze_end_tick"]],
        on=["demo_hash", "round_idx"],
        how="inner",
    )
    d["secs"] = (d["tick"] - d["freeze_end_tick"]) / TICKRATE
    bins = [-1, 15, 30, 45, 60, float("inf")]
    labels = ["0-15s", "15-30s", "30-45s", "45-60s", "60s+"]
    d["phase"] = pd.cut(d["secs"], bins=bins, labels=labels)

    kills = d[(d["attacker_steamid"].astype(str) == steamid)
              & (d["user_steamid"].astype(str) != steamid)]
    deaths = d[d["user_steamid"].astype(str) == steamid]
    if kills.empty and deaths.empty:
        return {}

    table = pd.DataFrame({
        "when": labels,
        "kills": [int((kills["phase"] == lb).sum()) for lb in labels],
        "deaths": [int((deaths["phase"] == lb).sum()) for lb in labels],
    })
    post = {}
    if "is_bomb_planted" in d.columns:
        post = {
            "kills_postplant_pct": float(kills["is_bomb_planted"].astype(bool).mean() * 100) if len(kills) else 0.0,
            "deaths_postplant_pct": float(deaths["is_bomb_planted"].astype(bool).mean() * 100) if len(deaths) else 0.0,
        }
    return {"table": table, **post}


def pistol_split(ledger: pd.DataFrame) -> dict:
    """Performance in pistol rounds (rounds 1 and 13 in MR12)."""
    if ledger.empty:
        return {}
    p = ledger[ledger["round_idx"].isin([0, 12])]
    if p.empty:
        return {}
    return {
        "rounds": len(p),
        "kills": int(p["kills"].sum()),
        "deaths": int(p["deaths"].sum()),
        "adr": float(p["damage"].sum() / len(p)),
        "win_pct": float(p["won"].mean() * 100),
    }


def bomb_actions(ms: MatchSet, steamid: str) -> dict:
    """How often the player is the one planting or defusing."""
    if ms.bombs.empty:
        return {"plants": 0, "defuses": 0}
    b = ms.bombs[ms.bombs["user_steamid"].astype(str) == str(steamid)]
    return {
        "plants": int((b["event"] == "bomb_planted").sum()),
        "defuses": int((b["event"] == "bomb_defused").sum()),
    }


def flash_stats(ms: MatchSet, steamid: str, n_rounds: int) -> dict:
    if ms.blinds.empty or not n_rounds:
        return {}
    b = ms.blinds
    enemy = b[
        (b["attacker_steamid"].astype(str) == str(steamid))
        & (b["attacker_team_num"] != b["user_team_num"])
        & (b["blind_duration"] > 0.7)
    ]
    got = b[(b["user_steamid"].astype(str) == str(steamid)) & (b["blind_duration"] > 0.7)]
    return {
        "enemies_flashed": len(enemy),
        "enemies_flashed_per_round": len(enemy) / n_rounds,
        "full_blinds": int((enemy["blind_duration"] > 2.5).sum()),
        "avg_blind_sec": float(enemy["blind_duration"].mean()) if len(enemy) else 0.0,
        "times_blinded": len(got),
    }


def utility_usage(ms: MatchSet, steamid: str, n_rounds: int) -> pd.DataFrame:
    """Grenades detonated by the player, per type, with per-round rate."""
    if ms.util.empty or not n_rounds:
        return pd.DataFrame()
    u = ms.util[ms.util["thrower_steamid"].astype(str) == str(steamid)]
    if u.empty:
        return pd.DataFrame()
    label = {
        "smokegrenade_detonate": "Smoke",
        "flashbang_detonate": "Flash",
        "hegrenade_detonate": "HE",
        "inferno_startburn": "Molotov",
        "decoy_detonate": "Decoy",
    }
    g = u.groupby("event").size().rename("thrown").reset_index()
    g["type"] = g["event"].map(label).fillna(g["event"])
    g["per round"] = (g["thrown"] / n_rounds).round(2)
    return g[["type", "thrown", "per round"]].sort_values("thrown", ascending=False)

from __future__ import annotations

import pandas as pd

from .loader import CT_SIDE, T_SIDE, MatchSet
from .killcontext import kill_log, kill_summary
from .positions import place_at, place_time, position_samples, transitions
from .stats import TICKRATE, first_bullet_accuracy, opening_detail, round_ledger
from .tactics import round_book, tactic_summary

UTIL_LABEL = {
    "smokegrenade_detonate": "smokes",
    "flashbang_detonate": "flashes",
    "hegrenade_detonate": "HE nades",
    "inferno_startburn": "molotovs",
    "decoy_detonate": "decoys",
}

# Advice thresholds — tuned for readability, every claim ships with its numbers anyway.
FB_STRONG, FB_WEAK = 55.0, 38.0
AWP_SHARE_MIN = 25.0
EARLY_PUSH_SEC = 18.0
LOW_TRADE_PCT = 25.0
MOLLY_HEAVY_PER_ROUND = 0.45


def _fmt_places(pairs: list[tuple[str, float]]) -> str:
    return ", ".join(f"{p} ({s:.0f}%)" for p, s in pairs)


def _util_rate(ms: MatchSet, steamids: set[str], side_num: int, n_rounds: int) -> dict:
    """Per-round utility counts and median throw-second for a set of players on one side."""
    out: dict[str, dict] = {}
    if ms.util.empty or not n_rounds or ms.rounds.empty:
        return out
    u = ms.util[
        ms.util["thrower_steamid"].astype(str).isin(steamids)
        & (ms.util["thrower_team_num"] == side_num)
    ]
    if u.empty:
        return out
    u = u.merge(
        ms.rounds[["demo_hash", "round_idx", "freeze_end_tick"]],
        on=["demo_hash", "round_idx"],
        how="inner",
    )
    u["sec"] = (u["tick"] - u["freeze_end_tick"]) / TICKRATE
    for event, grp in u.groupby("event"):
        label = UTIL_LABEL.get(str(event), str(event))
        out[label] = {
            "per_round": len(grp) / n_rounds,
            "median_sec": float(grp["sec"].median()),
            "count": len(grp),
        }
    return out


def side_profile(ms: MatchSet, steamid: str, name: str, side: str) -> dict:
    """All numbers needed to write advice about one player on one side."""
    steamid = str(steamid)
    side_num = T_SIDE if side == "T" else CT_SIDE
    ledger = round_ledger(ms, steamid)
    sled = ledger[ledger["side"] == side] if not ledger.empty else ledger
    prof: dict = {"name": name, "steamid": steamid, "side": side, "rounds": len(sled)}
    if sled.empty:
        return prof

    n = len(sled)
    kills, deaths = int(sled["kills"].sum()), int(sled["deaths"].sum())
    prof.update(
        kills=kills,
        deaths=deaths,
        kd=kills / max(deaths, 1),
        adr=float(sled["damage"].sum()) / n,
        opening_kills=int((sled["opening"] == "kill").sum()),
        opening_deaths=int((sled["opening"] == "death").sum()),
        traded_pct=float(sled.loc[sled["deaths"] > 0, "traded"].mean() * 100)
        if (sled["deaths"] > 0).any() else None,
        clutch_attempts=int(ledger["clutch"].notna().sum()),
        clutch_won=int(ledger["clutch"].dropna().str.contains("won").sum()),
    )

    samples = position_samples(ms, steamid, side=side)
    pt = place_time(samples, top=3)
    prof["top_places"] = list(zip(pt["area"], pt["share %"])) if not pt.empty else []
    for sec, key in ((15, "p15"), (30, "p30")):
        pa = place_at(samples, sec)
        prof[key] = (pa.iloc[0]["area"], pa.iloc[0]["share %"]) if not pa.empty else None
    tr = transitions(samples, top=3)
    prof["rotations"] = (
        list(zip(tr["from"], tr["to"], tr["times"], tr["typical time (s)"])) if not tr.empty else []
    )

    prof["kill_places"] = []
    prof["awp_share"] = 0.0
    if not ms.deaths.empty:
        mine = ms.deaths[
            (ms.deaths["attacker_steamid"].astype(str) == steamid)
            & (ms.deaths["user_steamid"].astype(str) != steamid)
        ]
        if "attacker_team_num" in mine.columns:
            side_kills = mine[mine["attacker_team_num"] == side_num]
        else:
            side_kills = mine
        if len(side_kills) and "attacker_last_place_name" in side_kills.columns:
            kp = side_kills["attacker_last_place_name"].astype(str)
            kp = kp[kp != ""].value_counts(normalize=True).head(3)
            prof["kill_places"] = [(p, s * 100) for p, s in kp.items()]
        if len(mine):
            prof["awp_share"] = float(
                (mine["weapon"].astype(str) == "awp").mean() * 100
            )

    fba = first_bullet_accuracy(ms, steamid)
    prof["fb_pct"] = fba.get("hit_pct") if fba else None
    prof["fb_n"] = fba.get("first_bullets", 0) if fba else 0

    od = opening_detail(ms, steamid)
    prof["open_sec"] = prof["open_place"] = None
    if not od.empty:
        ok = od[(od["side"] == side) & (od["role"] == "kill")]
        if len(ok):
            prof["open_sec"] = float(ok["sec"].median())
            mode = ok["place"][ok["place"] != ""].mode()
            prof["open_place"] = str(mode.iloc[0]) if len(mode) else None

    # Median second of their deaths on this side — late deaths on T suggest a lurker.
    prof["death_sec"] = None
    if not ms.deaths.empty and not ms.rounds.empty:
        d = ms.deaths[ms.deaths["user_steamid"].astype(str) == steamid]
        if "user_team_num" in d.columns:
            d = d[d["user_team_num"] == side_num]
        d = d.merge(ms.rounds[["demo_hash", "round_idx", "freeze_end_tick"]],
                    on=["demo_hash", "round_idx"], how="inner")
        if len(d):
            prof["death_sec"] = float(((d["tick"] - d["freeze_end_tick"]) / TICKRATE).median())

    prof["util"] = _util_rate(ms, {steamid}, side_num, n)

    log = kill_log(ms, steamid)
    prof["kill_ctx"] = kill_summary(log[log["side"] == side]) if not log.empty else {}
    return prof


def _kill_style_lines(profiles: list[dict]) -> list[str]:
    lines = []
    for p in profiles:
        ctx = p.get("kill_ctx") or {}
        if not ctx or ctx["n"] < 5:
            continue
        ww = ctx["where_when"]
        spot = ww.iloc[0] if len(ww) else None
        bits = []
        if spot is not None:
            bits.append(f"kills mostly at **{spot['place']}** around **{spot['typical sec']}s**")
        if ctx["holding_pct"] is not None:
            style = ("a **passive angle-holder** — clear his spots with utility, don't dry-swing"
                     if ctx["holding_pct"] >= 60
                     else "**mobile** — he swings and pushes; play for the re-peek and trades"
                     if ctx["holding_pct"] <= 30 else None)
            if style:
                bits.append(f"{ctx['holding_pct']:.0f}% of kills while holding still: {style}")
        if ctx["flash_pct"] >= 25:
            bits.append(f"**{ctx['flash_pct']:.0f}% of his kills come right after a team flash** — "
                        "turn away or counter-flash when you hear the pop")
        if ctx["smoke_pct"] >= 25 or ctx["through_smoke"] >= 2:
            bits.append(f"fights around smokes a lot ({ctx['smoke_pct']:.0f}%"
                        + (f", {ctx['through_smoke']} through-smoke kills" if ctx["through_smoke"] else "")
                        + ") — expect him to play smoke edges; pre-spam common smoke lines")
        if ctx["wallbangs"] >= 2:
            bits.append(f"{ctx['wallbangs']} wallbang kills — don't hug known spam walls")
        if len(ctx["routes"]):
            r = ctx["routes"].iloc[0]
            bits.append(f"common route: {r['came from']} → {r['place']} just before the kill")
        if bits:
            lines.append(f"**{p['name']}** ({ctx['n']} kills): " + "; ".join(bits) + ".")
    return lines


def _plan_vs_ct(profiles: list[dict], team_util: dict, ct_rounds: int, ct_win_pct: float | None) -> dict[str, list[str]]:
    """Advice for YOUR T side, derived from their CT tendencies."""
    s: dict[str, list[str]] = {
        "Duel targets": [], "Positions to check": [], "How they get their kills": [],
        "Aggression & pushes": [], "Utility to wait out": [], "Rotations & late round": [],
    }
    active = [p for p in profiles if p["rounds"]]
    if not active:
        return s
    s["How they get their kills"] = _kill_style_lines(active)

    fbs = [p for p in active if p.get("fb_pct") is not None and p["fb_n"] >= 10]
    if fbs:
        best = max(fbs, key=lambda p: p["fb_pct"])
        worst = min(fbs, key=lambda p: p["fb_pct"])
        if best["fb_pct"] >= FB_STRONG:
            s["Duel targets"].append(
                f"**Avoid dry-peeking {best['name']}** — {best['fb_pct']:.0f}% first-bullet accuracy "
                f"({best['fb_n']} shots). Flash him off his angle or trade-peek in pairs."
            )
        if worst["fb_pct"] <= FB_WEAK and worst is not best:
            s["Duel targets"].append(
                f"**Take your aim duels vs {worst['name']}** — only {worst['fb_pct']:.0f}% "
                f"first-bullet accuracy. Wide-peek him with confidence."
            )
    for p in active:
        if p["awp_share"] >= AWP_SHARE_MIN and p.get("kill_places"):
            s["Duel targets"].append(
                f"**{p['name']} is the AWP threat** ({p['awp_share']:.0f}% of his kills) — he holds "
                f"{_fmt_places(p['kill_places'])}. Smoke these lines or hit where he isn't."
            )
    isolated = sorted(
        [p for p in active
         if p.get("traded_pct") is not None and p["traded_pct"] <= LOW_TRADE_PCT and p["deaths"] >= 5],
        key=lambda p: p["traded_pct"],
    )[:2]
    for p in isolated:
        s["Duel targets"].append(
            f"{p['name']} plays isolated spots (only {p['traded_pct']:.0f}% of his deaths get "
            f"traded) — when you find him, commit: backup is far away."
        )

    for p in active:
        bits = []
        if p.get("p15"):
            bits.append(f"at 0:15 he's **{p['p15'][0]}** ({p['p15'][1]:.0f}% of rounds)")
        if p.get("p30") and (not p.get("p15") or p["p30"][0] != p["p15"][0]):
            bits.append(f"by 0:30 **{p['p30'][0]}** ({p['p30'][1]:.0f}%)")
        if p.get("kill_places"):
            bits.append(f"kills from {_fmt_places(p['kill_places'])}")
        if bits:
            s["Positions to check"].append(f"**{p['name']}**: " + "; ".join(bits) + ".")

    pushers = [p for p in active
               if p.get("open_sec") is not None and p["open_sec"] <= EARLY_PUSH_SEC and p["opening_kills"] >= 2]
    for p in pushers:
        where = f" around **{p['open_place']}**" if p.get("open_place") else ""
        s["Aggression & pushes"].append(
            f"**{p['name']} takes early fights** (~{p['open_sec']:.0f}s, {p['opening_kills']} opening "
            f"kills{where}) — pre-nade that contact spot or double-peek it at round start."
        )
    if not pushers:
        s["Aggression & pushes"].append(
            "No early CT aggression in the data — take early map control freely, "
            "their first contact comes from default holds."
        )

    for label, info in sorted(team_util.items(), key=lambda kv: -kv[1]["per_round"]):
        if label == "molotovs" and info["per_round"] >= MOLLY_HEAVY_PER_ROUND:
            s["Utility to wait out"].append(
                f"**Molly-heavy CT side**: {info['per_round']:.1f} molotovs/round, landing ~"
                f"{info['median_sec']:.0f}s — bait the early molly and hit during the 7s burn cooldown."
            )
        elif info["per_round"] >= 0.3:
            s["Utility to wait out"].append(
                f"{info['per_round']:.1f} {label}/round on CT, typically ~{info['median_sec']:.0f}s in."
            )
    if not s["Utility to wait out"]:
        s["Utility to wait out"].append("Light utility usage on CT — don't over-wait; hit on your timing.")

    for p in active:
        for frm, to, times, sec in p.get("rotations", [])[:1]:
            s["Rotations & late round"].append(
                f"{p['name']} rotates **{frm} → {to}** (~{sec}s, seen {times}×) — "
                f"fake toward {frm} to open up the other side."
            )
        if p["clutch_attempts"] >= 2 and p["clutch_won"] >= 1:
            s["Rotations & late round"].append(
                f"**{p['name']} clutches** ({p['clutch_won']}/{p['clutch_attempts']} won) — in late "
                f"rounds vs him: plant, set crossfires, play the clock. Don't hunt him one by one."
            )
    if ct_win_pct is not None:
        s["Rotations & late round"].append(
            f"Their CT side wins {ct_win_pct:.0f}% of rounds (n={ct_rounds}) — "
            + ("strong defense: trade discipline and util-first hits matter."
               if ct_win_pct >= 55 else "beatable defense: clean executes should break them.")
        )
    return s


def _plan_vs_t(profiles: list[dict], team_t: dict, team_util: dict) -> dict[str, list[str]]:
    """Advice for YOUR CT side, derived from their T tendencies."""
    s: dict[str, list[str]] = {
        "Sites & timing": [], "Entry threats": [], "How they get their kills": [],
        "Lurk & flank watch": [], "Their executes (utility)": [], "Economy reads": [],
    }
    active = [p for p in profiles if p["rounds"]]
    s["How they get their kills"] = _kill_style_lines(active)

    by_site = team_t.get("by_site")
    if by_site is not None and len(by_site):
        total = by_site["plants"].sum()
        parts = [f"**{r['plant site']} {r['plants'] / total * 100:.0f}%** (avg plant {r['avg_plant_sec']}s)"
                 for _, r in by_site.iterrows()]
        s["Sites & timing"].append("Plant split: " + " vs ".join(parts) + ".")
        fav = by_site.sort_values("plants", ascending=False).iloc[0]
        s["Sites & timing"].append(
            f"Lean your mid-round rotations toward **{fav['plant site']}**; expect the execute around "
            f"{max(float(fav['avg_plant_sec']) - 15, 10):.0f}s, so hold rotate decisions until then."
        )
    by_tactic = team_t.get("by_tactic")
    if by_tactic is not None and len(by_tactic):
        top = by_tactic.iloc[0]
        s["Sites & timing"].append(
            f"Most common pattern: **{top['tactic']}** ({top['share %']}% of T rounds, "
            f"wins {top['win %']}% of them)."
        )
    contact = team_t.get("contact_places")
    if contact is not None and len(contact):
        tops = ", ".join(f"**{r[0]}**" for r in contact.head(3).itertuples(index=False))
        s["Sites & timing"].append(f"First contact usually happens at {tops} — pre-aim/util these first.")

    entries = sorted([p for p in active if p["opening_kills"] >= 2],
                     key=lambda p: -p["opening_kills"])
    for p in entries[:2]:
        where = f" through **{p['open_place']}**" if p.get("open_place") else ""
        late = p.get("open_sec") is not None and p["open_sec"] > 35
        if late:
            s["Entry threats"].append(
                f"**{p['name']} wins the late first fight** ({p['opening_kills']}× opening kills{where}, "
                f"~{p['open_sec']:.0f}s) — mid-round patience play; don't peek him alone when it's quiet."
            )
        else:
            s["Entry threats"].append(
                f"**{p['name']} opens for them** ({p['opening_kills']} entry kills{where}"
                + (f", ~{p['open_sec']:.0f}s" if p.get("open_sec") is not None else "")
                + ") — set a crossfire there so his first kill gets refragged instantly."
            )
    feeders = [p for p in active if p["opening_deaths"] >= 3 and p["opening_deaths"] > p["opening_kills"]]
    for p in feeders[:2]:
        spot = f" near **{p['p15'][0]}**" if p.get("p15") else ""
        s["Entry threats"].append(
            f"{p['name']} dies first often ({p['opening_deaths']}×){spot} — an aggressive peek "
            f"toward him early is +EV."
        )
    if not s["Entry threats"]:
        s["Entry threats"].append("No standout entry player — they hit as a unit; focus on util-delay instead.")

    if active:
        team_med = pd.Series([p["death_sec"] for p in active if p.get("death_sec") is not None]).median()
        for p in active:
            if (p.get("death_sec") is not None and team_med is not None
                    and p["death_sec"] >= team_med + 15 and p.get("top_places")):
                s["Lurk & flank watch"].append(
                    f"**{p['name']} lurks** (dies ~{p['death_sec']:.0f}s vs team median {team_med:.0f}s) — "
                    f"usual ground: {_fmt_places(p['top_places'][:2])}. Watch that flank before rotating."
                )
    if not s["Lurk & flank watch"]:
        s["Lurk & flank watch"].append("No obvious lurker — they move together; flanks are lower risk.")

    util_bits = [f"~{info['per_round']:.1f} {label}/round (≈{info['median_sec']:.0f}s)"
                 for label, info in sorted(team_util.items(), key=lambda kv: -kv[1]["per_round"])
                 if info["per_round"] >= 0.2]
    if util_bits:
        s["Their executes (utility)"].append("Utility volume on T: " + ", ".join(util_bits) + ".")
        smoke = team_util.get("smokes")
        if smoke and smoke["per_round"] >= 0.8:
            s["Their executes (utility)"].append(
                f"Smoke-heavy executes (~{smoke['median_sec']:.0f}s) — **save your retake utility** and "
                f"consider one aggressive util-burn pick before their smokes go out."
            )
        else:
            s["Their executes (utility)"].append(
                "Thin executes — your site holds can fight through; punish dry hits."
            )

    by_buy = team_t.get("by_buy")
    if by_buy is not None and len(by_buy):
        for _, r in by_buy.iterrows():
            if r["buy"] in ("eco", "force") and r["rounds"] >= 2:
                s["Economy reads"].append(
                    f"On **{r['buy']}** rounds ({r['rounds']}×) they win {r['win %']}% — "
                    + ("respect their forces; don't over-push saves."
                       if r["win %"] >= 30 else "their saves rarely convert; play standard, deny exit frags.")
                )
        pistol = by_buy[by_buy["buy"] == "pistol"]
        if len(pistol):
            s["Economy reads"].append(
                f"Pistol rounds: {int(pistol.iloc[0]['wins'])}/{int(pistol.iloc[0]['rounds'])} won."
            )
    return s


def suggest_team(ms: MatchSet, anchor_sid: str) -> list[tuple[str, str]]:
    """The anchor player plus the 4 players who most often share his team."""
    anchor_sid = str(anchor_sid)
    if ms.econ.empty:
        return []
    e = ms.econ[["demo_hash", "round_idx", "steamid", "name", "team_num"]].copy()
    e["steamid"] = e["steamid"].astype(str)
    mine = e[e["steamid"] == anchor_sid][["demo_hash", "round_idx", "team_num"]]
    together = e.merge(mine, on=["demo_hash", "round_idx", "team_num"])
    counts = (
        together[together["steamid"] != anchor_sid]
        .groupby("steamid")
        .agg(name=("name", "last"), rounds=("round_idx", "count"))
        .sort_values("rounds", ascending=False)
    )
    anchor_name = e.loc[e["steamid"] == anchor_sid, "name"].iloc[-1]
    picks = [(anchor_sid, str(anchor_name))]
    picks += [(str(sid), str(r["name"])) for sid, r in counts.head(4).iterrows()]
    return picks


def battle_plan(ms: MatchSet, picks: list[tuple[str, str]], anchor_sid: str | None = None) -> dict:
    """Full rule-based gameplan against the picked players (steamid, name pairs).

    Returns profiles per side plus advice sections for your T and your CT halves.
    """
    if not picks:
        return {}
    sids = [str(s) for s, _ in picks]
    # anchor = pick with the most rounds; round_book/team stats hang off their team rounds
    if anchor_sid is None:
        roster = ms.roster()
        present = roster[roster["steamid"].isin(sids)]
        anchor_sid = str(present.iloc[0]["steamid"]) if len(present) else sids[0]

    ct_profiles = [side_profile(ms, sid, name, "CT") for sid, name in picks]
    t_profiles = [side_profile(ms, sid, name, "T") for sid, name in picks]

    anchor_ledger = round_ledger(ms, anchor_sid)
    ct_rounds = int((anchor_ledger["side"] == "CT").sum()) if not anchor_ledger.empty else 0
    ct_win = (float(anchor_ledger.loc[anchor_ledger["side"] == "CT", "won"].mean() * 100)
              if ct_rounds else None)
    t_rounds = int((anchor_ledger["side"] == "T").sum()) if not anchor_ledger.empty else 0

    book = round_book(ms, anchor_sid, side="T")
    team_t = tactic_summary(book) if not book.empty else {}

    util_ct = _util_rate(ms, set(sids), CT_SIDE, ct_rounds)
    util_t = _util_rate(ms, set(sids), T_SIDE, t_rounds)

    return {
        "anchor": anchor_sid,
        "ct_rounds": ct_rounds,
        "t_rounds": t_rounds,
        "vs_ct": {"profiles": ct_profiles, "sections": _plan_vs_ct(ct_profiles, util_ct, ct_rounds, ct_win)},
        "vs_t": {"profiles": t_profiles, "sections": _plan_vs_t(t_profiles, team_t, util_t)},
        "t_book": book,
    }

"""CS2 Scout — self-contained demo analytics website.

Run with:  streamlit run src/scout/ui/app.py
Everything is computed locally from parsed demos. No AI, no internet
(except a one-time radar image download per map).
"""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from scout.analytics import (
    DEMOS_DIR,
    MatchSet,
    accuracy,
    bomb_actions,
    first_bullet_accuracy,
    flash_stats,
    kill_timing,
    load_matchset,
    opening_detail,
    overview,
    parse_all,
    pistol_split,
    round_ledger,
    side_split,
    team_membership,
    utility_usage,
    weapon_tables,
)
from scout import auth
from scout.analytics.loader import cached_hashes, demo_status
from scout.analytics.positions import (
    PHASES,
    list_rounds,
    place_at,
    place_time,
    position_samples,
    round_path,
    transitions,
)
from scout.analytics.gameplan import battle_plan, suggest_team
from scout.analytics.killcontext import kill_log, kill_summary
from scout.analytics.tactics import round_book, tactic_summary
from scout.viz.heatmap import heatmap_figure, paths_figure, points_figure, split_by_level
from scout.viz.maps import has_calibration, map_levels

st.set_page_config(page_title="CS2 Scout", page_icon="🎯", layout="wide")


# ---------------------------------------------------------------- data access

def _fingerprint() -> tuple:
    """Cheap cache key: changes whenever the set of parsed demos changes."""
    return tuple(cached_hashes())


@st.cache_data(show_spinner="Loading parsed demos…")
def get_matchset(fingerprint: tuple) -> MatchSet:
    return load_matchset()


@st.cache_data(show_spinner="Crunching round ledger…")
def get_ledger(fingerprint: tuple, steamid: str, map_name: str | None) -> pd.DataFrame:
    ms = get_matchset(fingerprint).filtered(map_name)
    return round_ledger(ms, steamid)


def _website_url(srv_url: str | None) -> str:
    """Turn the ingest URL (…:8600) into the website URL (…:8501), robustly."""
    from urllib.parse import urlparse
    if not srv_url:
        return "your server"
    try:
        p = urlparse(srv_url if "://" in srv_url else f"http://{srv_url}")
        return f"{p.scheme or 'http'}://{p.hostname}:8501" if p.hostname else srv_url
    except ValueError:
        return srv_url


def show_fig(fig) -> None:
    st.pyplot(fig, width="stretch")
    plt.close(fig)


def player_picker(ms: MatchSet, key: str = "player") -> tuple[str, str] | None:
    roster = ms.roster()
    if roster.empty:
        return None
    labels = [
        f"{r['name']}  ({r['matches']} match{'es' if r['matches'] != 1 else ''}, {r['rounds']} rounds)"
        for _, r in roster.iterrows()
    ]
    stored = st.session_state.get("picked_steamid")
    default_idx = 0
    if stored is not None and (roster["steamid"] == stored).any():
        default_idx = int(roster.index[roster["steamid"] == stored][0])
    choice = st.selectbox("Player", labels, index=default_idx, key=key)
    row = roster.iloc[labels.index(choice)]
    st.session_state["picked_steamid"] = row["steamid"]
    return str(row["steamid"]), str(row["name"])


def map_filter(ms: MatchSet, steamid: str | None = None, key: str = "mapsel") -> str | None:
    maps = ms.player_maps(steamid) if steamid else ms.maps()
    options = ["All maps"] + maps
    choice = st.selectbox("Map", options, key=key)
    return None if choice == "All maps" else choice


def need_demos() -> bool:
    fp = _fingerprint()
    if not fp:
        st.info(
            "No parsed demos yet. Go to **📥 Demos**: drop `.dem` files into "
            f"`{DEMOS_DIR}` (any subfolder) or upload them there, then hit **Parse**."
        )
        return True
    return False


# ---------------------------------------------------------------- pages

def page_demos() -> None:
    st.header("📥 Demo manager")
    st.caption(
        f"Demos live under `{DEMOS_DIR}` — organize them into subfolders per team/event if "
        "you like. Drop files there directly, or upload below, then parse."
    )

    up_col, parse_col = st.columns([3, 2])
    with up_col:
        st.subheader("Upload demos")
        folder = st.text_input("Save uploads into subfolder", value="uploads")
        files = st.file_uploader("Drop .dem files here", type=["dem"], accept_multiple_files=True)
        if files:
            target = DEMOS_DIR / (folder.strip() or "uploads")
            target.mkdir(parents=True, exist_ok=True)
            saved = 0
            for f in files:
                dest = target / f.name
                if dest.exists() and dest.stat().st_size == f.size:
                    continue
                dest.write_bytes(f.getbuffer())
                saved += 1
            if saved:
                st.success(f"Saved {saved} demo(s) to {target}")

    with parse_col:
        st.subheader("Parse")
        force = st.checkbox("Force re-parse everything", value=False)
        if st.button("⚙️ Parse demos", type="primary", width="stretch"):
            from scout.ingest.faceit import prepare_compressed_demos
            bar = st.progress(0.0, text="Starting…")

            unpacked = prepare_compressed_demos(DEMOS_DIR)
            if unpacked:
                st.info(f"Unpacked {len(unpacked)} compressed .dem.zst/.gz demo(s).")

            def cb(i: int, n: int, name: str) -> None:
                bar.progress(i / max(n, 1), text=f"[{i}/{n}] {name}")

            results = parse_all(force=force, progress=cb)
            bar.progress(1.0, text="Done")
            errors = [r for r in results if r.get("error")]
            fresh = [r for r in results if not r.get("error") and not r.get("cached")]
            st.success(f"{len(results)} demo(s) checked — {len(fresh)} newly parsed.")
            for r in errors:
                st.error(f"{r['file']}: {r['error']}")
            st.cache_data.clear()
            st.rerun()

    st.subheader("Demo library")
    status = demo_status()
    if status.empty:
        st.warning("No .dem files found yet.")
        return
    st.dataframe(
        status.drop(columns=["hash"]),
        width="stretch",
        hide_index=True,
        column_config={"parsed": st.column_config.CheckboxColumn("parsed", disabled=True)},
    )
    parsed = status["parsed"].sum()
    st.caption(f"{parsed}/{len(status)} demos parsed. Parsed data is cached — new files only take a few seconds each.")


def render_player_overview(ms: MatchSet, steamid: str, name: str, map_name: str | None,
                           ledger: pd.DataFrame | None = None) -> dict | None:
    """Headline metrics + T/CT split for one player. Shared by the Player report
    page and the Battle plan's per-player section. Returns the overview dict."""
    if ledger is None:
        ledger = round_ledger(ms, steamid)
    if ledger.empty:
        st.info("No rounds for this player with the current filter.")
        return None
    ov = overview(ms, steamid, ledger)
    st.subheader(f"{name} — {ov['matches']} match(es), {ov['rounds']} rounds"
                 + (f" on {map_name}" if map_name else ""))
    row1 = st.columns(6)
    row1[0].metric("K / D", f"{ov['kills']} / {ov['deaths']}", f"{ov['kd']:.2f} KD")
    row1[1].metric("ADR", f"{ov['adr']:.0f}")
    row1[2].metric("KAST", f"{ov['kast']:.0f}%")
    row1[3].metric("Headshot %", f"{ov['hs_pct']:.0f}%")
    row1[4].metric("Kills / round", f"{ov['kpr']:.2f}")
    row1[5].metric("Round win %", f"{ov['win_pct']:.0f}%")

    row2 = st.columns(6)
    row2[0].metric("Opening kills", ov["opening_kills"],
                   f"{ov['opening_win_pct']:.0f}% rounds won", delta_color="off")
    row2[1].metric("Opening deaths", ov["opening_deaths"], delta_color="off")
    row2[2].metric("Clutches won", f"{ov['clutch_won']} / {ov['clutch_attempts']}")
    row2[3].metric("Death traded %", f"{ov['traded_pct']:.0f}%")
    row2[4].metric("Multi-kills", f"{ov['2k']}×2k {ov['3k']}×3k")
    row2[5].metric("Big rounds", f"{ov['4k']}×4k {ov['5k']}×ace")

    acc = accuracy(ms, steamid)
    fl = flash_stats(ms, steamid, ov["rounds"])
    fba = first_bullet_accuracy(ms, steamid)
    bombs = bomb_actions(ms, steamid)
    extra = st.columns(6)
    if fba:
        extra[0].metric("First-bullet accuracy", f"{fba['hit_pct']:.0f}%",
                        f"{fba['first_bullets']} first bullets", delta_color="off")
    if acc:
        extra[1].metric("Overall accuracy", f"{acc['accuracy_pct']:.0f}%",
                        f"{acc['hits']} hits / {acc['shots']} shots", delta_color="off")
    if fl:
        extra[2].metric("Enemies flashed / r", f"{fl['enemies_flashed_per_round']:.2f}")
        extra[3].metric("Full blinds", fl["full_blinds"])
    extra[4].metric("Bomb plants", bombs["plants"])
    extra[5].metric("Defuses", bombs["defuses"])

    st.markdown("**T vs CT**")
    st.dataframe(side_split(ledger), width="stretch", hide_index=True)
    return ov


def render_player_detail(ms: MatchSet, steamid: str, name: str, map_name: str) -> None:
    """Where/when they kill, weapons, and a duel map for one player — the tactical
    core of a player report, used in the Battle plan's per-player section."""
    log_df = kill_log(ms, steamid)
    if not log_df.empty:
        st.markdown("**Where & when they kill** (typical second of the round)")
        kc1, kc2 = st.columns(2)
        for side, col in (("T", kc1), ("CT", kc2)):
            with col:
                s = kill_summary(log_df[log_df["side"] == side])
                st.caption(f"{side} side")
                if s:
                    st.dataframe(s["where_when"], width="stretch", hide_index=True)
                else:
                    st.write("—")
    wcol, hcol = st.columns([1, 1])
    with wcol:
        wk, wd = weapon_tables(ms, steamid)
        if not wk.empty:
            st.markdown("**Kills by weapon**")
            st.dataframe(wk, width="stretch", hide_index=True)
    with hcol:
        if has_calibration(map_name) and not ms.deaths.empty:
            mk = ms.deaths[ms.deaths["attacker_steamid"].astype(str) == str(steamid)]
            md = ms.deaths[ms.deaths["user_steamid"].astype(str) == str(steamid)]
            st.markdown(f"**Duel map** — kills (green) & deaths (red) on {map_name}")
            show_fig(points_figure(
                [{"df": mk, "x": "attacker_X", "y": "attacker_Y", "color": "#2ecc71", "label": "kills"},
                 {"df": md, "x": "user_X", "y": "user_Y", "color": "#e74c3c", "label": "deaths", "marker": "X"}],
                map_name, f"{name} duels"))


def page_player() -> None:
    st.header("👤 Player report")
    if need_demos():
        return
    fp = _fingerprint()
    ms_all = get_matchset(fp)

    c1, c2 = st.columns([2, 1])
    with c1:
        picked = player_picker(ms_all)
    if not picked:
        st.warning("No players found in parsed demos.")
        return
    steamid, name = picked
    with c2:
        map_name = map_filter(ms_all, steamid)

    ms = ms_all.filtered(map_name)
    ledger = get_ledger(fp, steamid, map_name)
    if ledger.empty:
        st.warning("No rounds found for this player with the current filter.")
        return
    ov = render_player_overview(ms, steamid, name, map_name, ledger)
    fba = first_bullet_accuracy(ms, steamid)
    fl = flash_stats(ms, steamid, ov["rounds"])
    st.divider()

    tabs = st.tabs(
        ["🔥 Heatmaps", "📍 Positions & rotations", "🏃 Movement", "⚔️ Duels",
         "🔫 Weapons & utility", "🕒 Kill context"]
    )

    # ---- heatmaps
    with tabs[0]:
        hm_map = map_name or st.selectbox("Heatmap map", ms_all.player_maps(steamid), key="hm_map")
        if not has_calibration(hm_map):
            st.error(f"No radar calibration for `{hm_map}` yet — add it to scout/viz/maps.py "
                     "or data/radars/map-data.json.")
        else:
            msm = ms_all.filtered(hm_map)
            hc1, hc2 = st.columns(2)
            side = hc1.radio("Side", ["T", "CT", "Both"], horizontal=True, key="hm_side")
            phase = hc2.selectbox("Round phase", PHASES, key="hm_phase")
            side_arg = None if side == "Both" else side

            samples = position_samples(msm, steamid, side=side_arg, phase=phase)
            deaths = msm.deaths
            mine_k = mine_d = pd.DataFrame()
            if not deaths.empty:
                side_num = {"T": 2, "CT": 3}.get(side)
                mine_k = deaths[deaths["attacker_steamid"].astype(str) == steamid]
                mine_d = deaths[deaths["user_steamid"].astype(str) == steamid]
                if side_num:
                    if "attacker_team_num" in mine_k.columns:
                        mine_k = mine_k[mine_k["attacker_team_num"] == side_num]
                    if "user_team_num" in mine_d.columns:
                        mine_d = mine_d[mine_d["user_team_num"] == side_num]

            cols = st.columns(3)
            with cols[0]:
                st.markdown("**Presence** (where they spend time)")
                for level in map_levels(hm_map):
                    part = split_by_level(samples, "Z", hm_map).get(level, samples)
                    show_fig(heatmap_figure(part, "X", "Y", hm_map,
                                            f"{name} presence ({side}, {phase})",
                                            sigma=12, cmap="viridis", level=level))
            with cols[1]:
                st.markdown("**Kill positions** (where they shoot from)")
                for level in map_levels(hm_map):
                    part = split_by_level(mine_k, "attacker_Z", hm_map).get(level, mine_k)
                    show_fig(heatmap_figure(part, "attacker_X", "attacker_Y", hm_map,
                                            f"{name} kills ({side})", level=level))
            with cols[2]:
                st.markdown("**Death positions** (where they die)")
                for level in map_levels(hm_map):
                    part = split_by_level(mine_d, "user_Z", hm_map).get(level, mine_d)
                    show_fig(heatmap_figure(part, "user_X", "user_Y", hm_map,
                                            f"{name} deaths ({side})", cmap="cool", level=level))

            st.markdown("**Duel map** — exact kill (green) and death (red) spots")
            for level in map_levels(hm_map):
                k_part = split_by_level(mine_k, "attacker_Z", hm_map).get(level, mine_k)
                d_part = split_by_level(mine_d, "user_Z", hm_map).get(level, mine_d)
                show_fig(points_figure(
                    [
                        {"df": k_part, "x": "attacker_X", "y": "attacker_Y",
                         "color": "#2ecc71", "label": "kills"},
                        {"df": d_part, "x": "user_X", "y": "user_Y",
                         "color": "#e74c3c", "label": "deaths", "marker": "X"},
                    ],
                    hm_map, f"{name} duels ({side})", level=level,
                ))

    # ---- positions & rotations
    with tabs[1]:
        pc1, pc2 = st.columns(2)
        for side, col in (("T", pc1), ("CT", pc2)):
            with col:
                st.markdown(f"### {side} side")
                samples = position_samples(ms, steamid, side=side)
                pt = place_time(samples)
                if pt.empty:
                    st.info("No position data.")
                    continue
                st.markdown("**Most played areas** (share of alive time)")
                st.dataframe(pt, width="stretch", hide_index=True,
                             column_config={"share %": st.column_config.ProgressColumn(
                                 "share %", min_value=0, max_value=float(pt['share %'].max()),
                                 format="%.1f%%")})
                st.markdown("**Default position** — where they stand at…")
                for sec in (15, 30, 50):
                    pa = place_at(samples, sec)
                    if not pa.empty:
                        top = pa.iloc[0]
                        st.write(f"• **{sec}s**: {top['area']} ({top['share %']:.0f}% of rounds)"
                                 + (f", then {pa.iloc[1]['area']} ({pa.iloc[1]['share %']:.0f}%)"
                                    if len(pa) > 1 else ""))
                st.markdown("**Common rotations** (area → area)")
                tr = transitions(samples)
                if not tr.empty:
                    st.dataframe(tr, width="stretch", hide_index=True)

    # ---- movement
    with tabs[2]:
        mv_map = map_name or st.selectbox("Map", ms_all.player_maps(steamid), key="mv_map")
        msm = ms_all.filtered(mv_map)
        mc1, mc2 = st.columns([1, 3])
        side = mc1.radio("Side", ["T", "CT"], horizontal=True, key="mv_side")
        rounds = list_rounds(msm, steamid, side=side)
        if rounds.empty or not has_calibration(mv_map):
            st.info("No movement data for this selection.")
        else:
            labels = [
                f"{r.demo_label} R{int(r.round_idx) + 1} ({'won' if r.won else 'lost'}"
                + (f", plant {r.plant_site}" if pd.notna(r.plant_site) else "") + ")"
                for r in rounds.itertuples()
            ]
            default = labels[: min(8, len(labels))]
            chosen = mc2.multiselect("Rounds to draw", labels, default=default, key="mv_rounds")
            paths = []
            for lab in chosen:
                r = rounds.iloc[labels.index(lab)]
                p = round_path(msm, steamid, r["demo_hash"], r["round_idx"])
                if len(p) > 1:
                    paths.append({"x": p["X"], "y": p["Y"], "label": f"R{int(r['round_idx']) + 1}"})
            if paths:
                show_fig(paths_figure(paths, mv_map,
                                      f"{name} {side}-side movement (start ●, end ✕)"))
            st.caption("Dot = round start position, X = where the round ended for them. "
                       "Pick fewer rounds to see individual routes clearly.")

    # ---- duels
    with tabs[3]:
        od = opening_detail(ms, steamid)
        dc1, dc2 = st.columns(2)
        with dc1:
            st.markdown("**Opening duels** (first blood of the round)")
            if od.empty:
                st.info("No opening duels.")
            else:
                st.dataframe(od.rename(columns={"round_idx": "round"}),
                             width="stretch", hide_index=True)
        with dc2:
            st.markdown("**Clutch situations**")
            cl = ledger[ledger["clutch"].notna()][
                ["demo_label", "map_name", "round_idx", "side", "clutch", "kills"]
            ].rename(columns={"round_idx": "round", "kills": "kills that round"})
            if cl.empty:
                st.info("No 1vX situations found.")
            else:
                cl["round"] = cl["round"] + 1
                st.dataframe(cl, width="stretch", hide_index=True)
            st.markdown("**Best rounds**")
            best = ledger.sort_values(["kills", "damage"], ascending=False).head(5)[
                ["demo_label", "round_idx", "side", "kills", "damage", "won"]
            ].rename(columns={"round_idx": "round"})
            best["round"] = best["round"] + 1
            best["damage"] = best["damage"].round(0).astype(int)
            st.dataframe(best, width="stretch", hide_index=True)

        st.divider()
        tc1, tc2 = st.columns(2)
        with tc1:
            st.markdown("**Kill / death timing** (seconds into the round)")
            kt = kill_timing(ms, steamid)
            if kt:
                st.dataframe(kt["table"], width="stretch", hide_index=True)
                if "kills_postplant_pct" in kt:
                    st.caption(f"{kt['kills_postplant_pct']:.0f}% of kills and "
                               f"{kt['deaths_postplant_pct']:.0f}% of deaths happen after the plant.")
        with tc2:
            st.markdown("**Pistol rounds** (rounds 1 & 13)")
            pr = pistol_split(ledger)
            if pr:
                pcols = st.columns(3)
                pcols[0].metric("K / D", f"{pr['kills']} / {pr['deaths']}")
                pcols[1].metric("ADR", f"{pr['adr']:.0f}")
                pcols[2].metric("Win %", f"{pr['win_pct']:.0f}%",
                                f"{pr['rounds']} pistol rounds", delta_color="off")
            else:
                st.info("No pistol rounds in this selection.")

    # ---- weapons & utility
    with tabs[4]:
        if fba:
            st.markdown("**🎯 First-bullet accuracy by weapon** — the shot taken after ≥1s "
                        "of not firing; pure crosshair placement, no spray.")
            ac1, ac2, ac3 = st.columns(3)
            ac1.metric("First-bullet hit %", f"{fba['hit_pct']:.0f}%")
            ac2.metric("First-bullet headshot %", f"{fba['head_pct']:.0f}%")
            wk_tmp, _ = weapon_tables(ms, steamid)
            if not wk_tmp.empty:
                awp_kills = int(wk_tmp.loc[wk_tmp["weapon"] == "awp", "count"].sum())
                total_kills = int(wk_tmp["count"].sum())
                if total_kills:
                    ac3.metric("AWP kill share", f"{awp_kills / total_kills * 100:.0f}%")
            bw = fba["by_weapon"]
            st.dataframe(bw[bw["first bullets"] >= 10] if len(bw[bw["first bullets"] >= 10]) else bw,
                         width="stretch", hide_index=True)
            st.divider()

        wk, wd = weapon_tables(ms, steamid)
        wc1, wc2, wc3 = st.columns(3)
        with wc1:
            st.markdown("**Kills by weapon**")
            if not wk.empty:
                st.dataframe(wk, width="stretch", hide_index=True)
        with wc2:
            st.markdown("**What kills them**")
            if not wd.empty:
                st.dataframe(wd, width="stretch", hide_index=True)
        with wc3:
            st.markdown("**Utility per round**")
            uu = utility_usage(ms, steamid, ov["rounds"])
            if uu.empty:
                st.info("No utility data.")
            else:
                st.dataframe(uu, width="stretch", hide_index=True)
            if fl:
                st.write(f"Enemies flashed: **{fl['enemies_flashed']}** "
                         f"(avg {fl['avg_blind_sec']:.1f}s), "
                         f"times blinded themselves: **{fl['times_blinded']}**")
            else:
                st.caption("This demo type doesn't record flash-blind events.")

    # ---- kill context
    with tabs[5]:
        log = kill_log(ms, steamid)
        if log.empty:
            st.info("No kills in this selection.")
        else:
            kc1, kc2 = st.columns(2)
            for side, col in (("T", kc1), ("CT", kc2)):
                with col:
                    st.markdown(f"### {side}-side kills")
                    s = kill_summary(log[log["side"] == side])
                    if not s:
                        st.info("No kills.")
                        continue
                    m1, m2, m3 = st.columns(3)
                    if s["holding_pct"] is not None:
                        m1.metric("Holding an angle", f"{s['holding_pct']:.0f}%",
                                  "vs moving/pushing", delta_color="off")
                    m2.metric("Off a team flash", f"{s['flash_pct']:.0f}%")
                    m3.metric("Around smokes", f"{s['smoke_pct']:.0f}%",
                              f"{s['through_smoke']} through smoke", delta_color="off")
                    st.markdown("**Where & when they kill** (typical second of the round)")
                    st.dataframe(s["where_when"], width="stretch", hide_index=True)
                    if len(s["routes"]):
                        st.markdown("**Route just before the kill** (position 10s earlier → kill spot)")
                        st.dataframe(s["routes"], width="stretch", hide_index=True)
                    extras = []
                    if s["wallbangs"]:
                        extras.append(f"{s['wallbangs']} wallbang kills")
                    if s["noscopes"]:
                        extras.append(f"{s['noscopes']} noscopes")
                    if extras:
                        st.caption(" · ".join(extras))
            with st.expander("Full kill log (every kill, with context)"):
                st.dataframe(log, width="stretch", hide_index=True)


def page_tactics() -> None:
    st.header("💣 Team round tactics")
    if need_demos():
        return
    fp = _fingerprint()
    ms_all = get_matchset(fp)

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        picked = player_picker(ms_all, key="tact_player")
    if not picked:
        return
    steamid, name = picked
    with c2:
        map_name = map_filter(ms_all, steamid, key="tact_map")
    with c3:
        side = st.radio("Side", ["T", "CT"], horizontal=True, key="tact_side")

    ms = ms_all.filtered(map_name)
    book = round_book(ms, steamid, side=side)
    if book.empty:
        st.warning("No rounds for this selection.")
        return

    st.caption(f"Analyzing the **team {name} plays on** — {len(book)} {side}-side rounds.")
    summ = tactic_summary(book)

    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        st.markdown("**What they run** (win rate per pattern)")
        st.dataframe(summ["by_tactic"], width="stretch", hide_index=True)
    with sc2:
        st.markdown("**By buy type**")
        st.dataframe(summ["by_buy"], width="stretch", hide_index=True)
        if "by_site" in summ:
            st.markdown("**Bomb plants**")
            st.dataframe(summ["by_site"], width="stretch", hide_index=True)
    with sc3:
        if "contact_places" in summ:
            st.markdown("**Where first contact happens**")
            st.dataframe(summ["contact_places"], width="stretch", hide_index=True)

    st.subheader("Round by round")
    st.dataframe(
        book.drop(columns=["demo_hash", "round_idx"]),
        width="stretch",
        hide_index=True,
    )
    st.caption("`spread @20s` = where the five players were standing 20 seconds into the round — "
               "their default setup. `Fast` = bomb planted before 45s.")


def _profile_table(profiles: list[dict]) -> pd.DataFrame:
    rows = []
    for p in profiles:
        if not p.get("rounds"):
            continue
        rows.append({
            "player": p["name"],
            "rounds": p["rounds"],
            "K/D": round(p["kd"], 2),
            "ADR": round(p["adr"], 0),
            "entry K-D": f"{p['opening_kills']}-{p['opening_deaths']}",
            "first bullet %": round(p["fb_pct"], 0) if p.get("fb_pct") is not None else None,
            "AWP %": round(p["awp_share"], 0),
            "clutches": f"{p['clutch_won']}/{p['clutch_attempts']}",
            "@0:15": p["p15"][0] if p.get("p15") else "",
        })
    return pd.DataFrame(rows)


def page_battle() -> None:
    st.header("⚔️ Battle plan")
    if need_demos():
        return
    fp = _fingerprint()
    ms_all = get_matchset(fp)

    maps = ms_all.maps()
    qp = st.query_params
    # deep link (?map=…): preselect the map on first load, before the widget exists
    if "bp_map" not in st.session_state and qp.get("map") in maps:
        st.session_state["bp_map"] = qp.get("map")
    scout_map = st.session_state.get("scout_map")
    map_idx = maps.index(scout_map) if scout_map in maps else 0
    c1, c2 = st.columns([1, 3])
    with c1:
        bp_map = st.selectbox("Map", maps, index=map_idx, key="bp_map")
    ms = ms_all.filtered(bp_map)
    roster = ms.roster()
    if roster.empty:
        st.warning("No players on this map.")
        return

    labels = {f"{r['name']} ({r['rounds']} rounds)": str(r["steamid"]) for _, r in roster.iterrows()}
    label_by_sid = {sid: label for label, sid in labels.items()}
    # deep link (?team=sid1,sid2,…): preselect the 5 players + anchor on first load
    if "bp_players" not in st.session_state and qp.get("team"):
        want = [label_by_sid[s] for s in qp.get("team").split(",") if s in label_by_sid]
        if want:
            st.session_state["bp_players"] = want
            st.session_state.setdefault("bp_anchor", want[0])
    scouted = st.session_state.get("scout_anchor")
    anchor_idx = 0
    if scouted:
        for i, sid in enumerate(labels.values()):
            if sid == str(scouted):
                anchor_idx = i
                break
    with c2:
        anchor_label = st.selectbox(
            "Enemy team — pick any player from it (their 4 most frequent teammates auto-fill)",
            list(labels), index=anchor_idx, key="bp_anchor",
        )
    anchor_sid = labels[anchor_label]
    default_labels = [label_by_sid[s] for s, _ in suggest_team(ms, anchor_sid) if s in label_by_sid]

    chosen = st.multiselect("The 5 enemies", list(labels), default=default_labels,
                            max_selections=5, key="bp_players")
    if not chosen:
        st.info("Pick 1–5 players.")
        return
    picks = [(labels[c], c.rsplit(" (", 1)[0]) for c in chosen]

    plan = battle_plan(ms, picks, anchor_sid=anchor_sid)
    if not plan:
        st.warning("Not enough data.")
        return
    st.caption(
        f"Based on **{plan['ct_rounds']} CT rounds / {plan['t_rounds']} T rounds** of theirs on "
        f"{bp_map}. Every claim shows its numbers — small samples mean softer reads."
        + (" ⚠️ Under 20 rounds per side: treat as hints, not gospel."
           if min(plan["ct_rounds"], plan["t_rounds"]) < 20 else "")
    )

    palette = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231"]
    tab_t, tab_ct = st.tabs(["🗡️ You play T (their CT)", "🛡️ You play CT (their T)"])
    for tab, key in ((tab_t, "vs_ct"), (tab_ct, "vs_t")):
        side_letter = "CT" if key == "vs_ct" else "T"
        side_num = 3 if side_letter == "CT" else 2
        with tab:
            for section, lines in plan[key]["sections"].items():
                if not lines:
                    continue
                st.subheader(section)
                st.markdown("\n".join(f"- {line}" for line in lines))
            if has_calibration(bp_map):
                with st.expander("📍 Show their positions on the map", expanded=False):
                    sec = st.slider("Second of the round", 5, 90, 15, 5, key=f"bp_sec_{key}")
                    pos_layers, kill_layers = [], []
                    for i, (sid, nm) in enumerate(picks):
                        samp = position_samples(ms, sid, side=side_letter)
                        if not samp.empty:
                            w = samp[(samp["secs"] >= sec - 3) & (samp["secs"] <= sec + 3)]
                            pos_layers.append({"df": w, "x": "X", "y": "Y",
                                               "color": palette[i % 5], "label": nm})
                        if not ms.deaths.empty:
                            mk = ms.deaths[
                                (ms.deaths["attacker_steamid"].astype(str) == sid)
                                & (ms.deaths["attacker_team_num"] == side_num)
                            ]
                            kill_layers.append({"df": mk, "x": "attacker_X", "y": "attacker_Y",
                                                "color": palette[i % 5], "label": nm, "marker": "X"})
                    mc1, mc2 = st.columns(2)
                    with mc1:
                        show_fig(points_figure(pos_layers, bp_map,
                                               f"Their {side_letter} positions at ~{sec}s"))
                    with mc2:
                        show_fig(points_figure(kill_layers, bp_map,
                                               f"Where their {side_letter} kills come from"))
                    st.caption("One color per player. Drag the slider to watch their default "
                               "setup unfold second by second.")
            prof_df = _profile_table(plan[key]["profiles"])
            if not prof_df.empty:
                st.subheader("The numbers")
                st.dataframe(prof_df, width="stretch", hide_index=True)

    st.divider()
    st.subheader(f"📋 Individual player reports — {bp_map}")
    st.caption("Full per-player breakdown on this map, just like the Player report page.")
    name_to_sid = {nm: sid for sid, nm in picks}
    sel = st.radio("Player", list(name_to_sid), horizontal=True, key="bp_player_report")
    sid = name_to_sid[sel]
    render_player_overview(ms, sid, sel, bp_map)
    render_player_detail(ms, sid, sel, bp_map)


def page_autoscout() -> None:
    from scout.ingest import browser_runner, faceit, push

    st.header("🔎 Auto-scout (FACEIT)")
    st.caption(
        "Paste your match room link → it finds your team by your nickname, scouts the **other** "
        "team on the room's map, downloads their demos, parses, and preps the battle plan."
    )

    key = faceit.load_key()
    nick = faceit.load_nick()
    proxy = faceit.load_proxy()
    with st.expander("⚙️ Settings (API key, nickname & demo proxy)",
                     expanded=key is None or not nick):
        st.markdown(
            "Free key at [developers.faceit.com](https://developers.faceit.com) → create app → "
            "*API keys* → server-side key. Your nickname detects which team is yours."
        )
        sc1, sc2 = st.columns(2)
        new_key = sc1.text_input("FACEIT API key", value=key or "", type="password")
        if new_key and new_key != key:
            faceit.save_key(new_key)
            key = new_key
            st.success("Key saved.")
        new_nick = sc2.text_input("Your FACEIT nickname", value=nick or "")
        if new_nick and new_nick != nick:
            faceit.save_nick(new_nick)
            nick = new_nick
            st.success(f"Saved — you are **{nick}**.")

        st.divider()
        st.markdown(
            "**Demo download proxy** (optional) — if the demo CDN is blocked on your network, "
            "route **only the demo download** through an EU proxy (e.g. Amsterdam). Your API key "
            "is never sent through it. Formats: `socks5://host:port`, `http://host:port`, or with "
            "auth `socks5://user:pass@host:port`."
        )
        pc1, pc2 = st.columns([3, 1])
        new_proxy = pc1.text_input("Proxy URL", value=proxy or "",
                                   placeholder="socks5://127.0.0.1:1080")
        if new_proxy != (proxy or ""):
            faceit.save_proxy(new_proxy)
            proxy = new_proxy or None
            st.success("Proxy saved." if proxy else "Proxy cleared.")
        if pc2.button("🔌 Test connection", width="stretch"):
            with st.spinner("Testing CDN reachability…"):
                ok, msg = faceit.test_cdn(proxy=proxy)
            (st.success if ok else st.error)(msg)

        st.divider()
        logged_in = browser_runner.is_logged_in()
        st.markdown(
            "**🔐 Full auto-download (browser)** — FACEIT only serves demos to a logged-in browser. "
            + ("✅ **Logged in** — a session is saved." if logged_in
               else "⚠️ **Not logged in yet.**")
        )
        st.warning(
            "Clicking below opens a **real Chrome window** (your installed Chrome, launched cleanly "
            "so Cloudflare treats it as a normal browser — not an automated one). "
            "**Log into FACEIT in that window, then leave it open.** Your everyday browser's login "
            "doesn't carry over. You only log in once; the session is saved and reused for every "
            "future scout.",
            icon="🪟",
        )
        st.caption("Run this app on the same PC. If downloads later say “session expired”, click Log in again.")
        bcol1, bcol2 = st.columns([1, 2])
        if bcol1.button("🌐 Log in to FACEIT", width="stretch", type="primary"):
            log = st.status("Opening Chrome — log in to FACEIT in the window that appears…",
                            expanded=True)
            ok = False
            for ev in browser_runner.run("login"):
                log.write(ev.get("msg", ev.get("phase", "")))
                if ev.get("phase") == "logged_in":
                    ok = True
            if ok:
                log.update(label="Logged in — session saved ✓", state="complete")
                st.rerun()
            else:
                log.update(label="Login not completed — try again", state="error")
        if logged_in:
            bcol2.success("Auto-download is on. Just run the scout below.")

        st.divider()
        srv_url, srv_token = push.load_server()
        st.markdown(
            "**🖥️ Offload to a server** (optional) — if set, your PC only signs the links and the "
            "server (fast, in-EU) does the 300 MB download + parse; view results on its website. "
            "Auth is by **IP whitelist** on the server (token optional). Leave blank to work locally."
        )
        new_url = st.text_input("Server URL", value=srv_url or "",
                                 placeholder="http://SERVER_IP:8600")
        new_token = st.text_input("Ingest token (only if your server sets one — usually blank)",
                                  value=srv_token or "", type="password")
        if new_url.strip() != (srv_url or "") or new_token.strip() != (srv_token or ""):
            push.save_server(new_url, new_token)
            srv_url, srv_token = push.load_server()
            st.success("Server saved." if srv_url else "Server cleared.")
        gc1, gc2 = st.columns(2)
        if gc1.button("🔌 Test server", width="stretch", disabled=not srv_url):
            ok, msg = push.server_health(srv_url)
            (st.success if ok else st.error)(msg)
        if gc2.button("🪪 What's my IP? (to whitelist)", width="stretch", disabled=not srv_url):
            ok, msg = push.whoami(srv_url)
            (st.success if ok else st.error)(msg)
    if not key:
        st.info("Enter your API key above to continue.")
        return

    url = st.text_input("Match room link (or match id)",
                        placeholder="https://www.faceit.com/en/cs2/room/1-…")
    if not url.strip():
        return
    match_id = faceit.parse_match_id(url)

    if st.button("Fetch match", type="primary"):
        try:
            st.session_state["scout_details"] = faceit.match_details(match_id, key)
        except Exception as e:
            st.error(f"Could not fetch match: {e}")
            return
    details = st.session_state.get("scout_details")
    if not details:
        return

    teams = faceit.rosters(details)
    mmap = faceit.match_map(details)
    sides = faceit.find_sides(details, nick) if nick else None

    if sides and sides["enemy_team"]:
        enemy_name = sides["enemy_team"]
        enemy = sides["enemy_players"]
        st.success(
            f"**You:** {nick} ({sides['my_team']})  ·  **Opponents:** {enemy_name}  ·  "
            f"**Map:** `{mmap or 'not picked yet'}`"
        )
        st.write("Scouting: " + ", ".join(p["nickname"] for p in enemy))
    else:
        if nick:
            st.warning(f"Couldn't find **{nick}** in this match — pick the opponent team manually.")
        else:
            st.info("Set your nickname in Settings to auto-detect your team.")
        enemy_name = st.radio("Which team are you scouting (the opponents)?",
                              list(teams), horizontal=True)
        enemy = teams[enemy_name]
        st.write(", ".join(p["nickname"] for p in enemy))

    map_pool = ("de_mirage", "de_ancient", "de_dust2", "de_inferno",
                "de_nuke", "de_anubis", "de_train", "de_overpass")
    with st.expander("Advanced options"):
        a1, a2, a3 = st.columns(3)
        only_room_map = a1.checkbox(f"Only the room map ({mmap})" if mmap else "Only the room map",
                                    value=bool(mmap))
        override_map = a2.selectbox("…or a specific map", ["(use room map)", "any map"] + list(map_pool),
                                    disabled=only_room_map)
        per_player = a3.slider("Recent matches per player", 1, 5, 1)
        cap = a1.slider("Max demos total", 2, 15, 8)

    if only_room_map:
        map_only = mmap
    elif override_map == "any map":
        map_only = None
    elif override_map == "(use room map)":
        map_only = mmap
    else:
        map_only = override_map
    map_label = map_only or "any map"

    if st.button(f"⚔️ Scout {enemy_name} on {map_label}", type="primary"):
        log = st.status("Scouting…", expanded=True)
        dl_bar = st.progress(0.0, text="Preparing…")

        def on_progress(info: dict) -> None:
            phase = info.get("phase")
            count = info.get("count", 0)
            if phase == "downloading_parallel":
                td, tt = info.get("downloaded", 0), info.get("total", 0)
                if tt:
                    dl_bar.progress(min(td / tt, 1.0),
                                    text=f"⬇️ {count} demos in parallel · "
                                         f"{td / 1e6:.0f} / {tt / 1e6:.0f} MB")
                else:
                    dl_bar.progress(0.0, text=f"⬇️ downloading {count} demos in parallel…")
                return
            if phase == "remote":
                dl_bar.progress(0.05, text=f"🖥️ sending {count} link(s) to the server…")
                return
            if phase == "server_download":
                td, tt = info.get("downloaded", 0), info.get("total", 0)
                if tt:
                    dl_bar.progress(min(0.1 + 0.6 * td / tt, 0.7),
                                    text=f"🖥️ server downloading · {td / 1e6:.0f} / {tt / 1e6:.0f} MB")
                else:
                    dl_bar.progress(0.1, text="🖥️ server downloading…")
                return
            if phase == "server_parse":
                i, n = info.get("i", 0), info.get("n", 0)
                frac = 0.7 + 0.25 * (i / n if n else 0)
                dl_bar.progress(min(frac, 0.97),
                                text=f"🖥️ server parsing · {i}/{n} demos")
                return
            who = info.get("nickname") or "?"
            idx = info.get("index") or 0
            mmap_i = info.get("map") or "?"
            if phase == "searching":
                dl_bar.progress(0.0, text=f"🔎 searching {who}'s history for "
                                          f"{map_label} (checked {info['checked']}, last: {mmap_i})…")
            elif phase == "checking":
                dl_bar.progress((idx - 1) / max(count, 1),
                                text=f"[{idx}/{count}] checking {who}'s match…")
            elif phase == "start":
                dl_bar.progress((idx - 1) / max(count, 1),
                                text=f"[{idx}/{count}] {who} · {mmap_i} · connecting…")
            elif phase == "downloading":
                done_mb = info["downloaded"] / 1e6
                total = info["total"]
                if total:
                    # bar reflects overall progress: finished demos + this demo's fraction
                    frac = (idx - 1 + info["downloaded"] / total) / max(count, 1)
                    dl_bar.progress(min(frac, 1.0),
                                    text=f"[{idx}/{count}] {who} · {mmap_i} · "
                                         f"{done_mb:.0f} / {total / 1e6:.0f} MB")
                else:
                    dl_bar.progress((idx - 1) / max(count, 1),
                                    text=f"[{idx}/{count}] {who} · {mmap_i} · {done_mb:.0f} MB")
            elif phase == "cached":
                dl_bar.progress(idx / max(count, 1), text=f"[{idx}/{count}] {who} · already downloaded")
            elif phase == "done":
                dl_bar.progress(idx / max(count, 1), text=f"[{idx}/{count}] {who} · {mmap_i} · saved ✓")
            elif phase == "skipped":
                log.write(f"⏭️ {who}'s match skipped (map {mmap_i})")
            elif phase == "error":
                log.write(f"⚠️ {who}'s match failed")

        srv_url, srv_token = push.load_server()
        use_remote = bool(srv_url) and browser_runner.is_logged_in()
        if browser_runner.is_logged_in():
            report = faceit.scout_opponents_browser(
                match_id, key, enemy, per_player=per_player, map_filter=map_only,
                total_cap=cap, proxy=proxy,
                remote=({"url": srv_url, "token": srv_token} if use_remote else None),
                progress=lambda msg: log.write(msg), on_progress=on_progress)
        else:
            report = faceit.scout_opponents(
                match_id, key, enemy, per_player=per_player, map_filter=map_only,
                total_cap=cap, proxy=proxy,
                progress=lambda msg: log.write(msg), on_progress=on_progress)
        n_dl = len(report["downloaded"])
        n_skip = len(report["skipped"])
        n_err = len(report["errors"])
        links = report.get("links") or []
        dl_bar.progress(1.0, text=f"{n_dl} done · {len(links)} found")
        log.update(label=f"{len(links)} demos found, {n_dl} processed", state="complete")

        # if the browser session was tried and failed, show why
        for mid, err in report["errors"]:
            st.error(f"❌ `{mid[:13]}…` — {err}")

        if report.get("remote"):
            if report["downloaded"]:
                n_new = sum(1 for d in report["downloaded"] if not d.get("cached"))
                n_cached = n_dl - n_new
                tail = f", {n_cached} already on the server" if n_cached else ""
                sids = ",".join(p["steamid"] for p in enemy if p.get("steamid"))
                deep = (f"{_website_url(srv_url)}/?view=battle"
                        f"&map={map_only or ''}&team={sids}")
                st.success(f"✅ Server ingested {n_new} new demo(s){tail}.")
                st.markdown(f"### 👉 [Open the battle plan for {enemy_name}]({deep})")
                st.caption(f"Opens {enemy_name} on `{map_only or 'the scouted map'}`, "
                           "preselected — you'll just enter the login code.")
            else:
                st.warning("Nothing was ingested on the server — see errors above.")
        elif report["downloaded"]:
            st.write("Parsing demos…")
            pbar = st.progress(0.0, text="Parsing…")
            parse_all(progress=lambda i, n, name: pbar.progress(
                i / max(n, 1), text=f"parsing [{i}/{n}] {name}"))
            pbar.progress(1.0, text="parsed ✓")
            st.cache_data.clear()
            anchor = next((p["steamid"] for p in enemy if p["steamid"]), None)
            if anchor:
                st.session_state["scout_anchor"] = anchor
            scouted_map = map_only or mmap
            if scouted_map:
                st.session_state["scout_map"] = scouted_map
                st.session_state["bp_map"] = scouted_map  # preselect the Battle plan map
            if "⚔️ Battle plan" in _pages_for_role():
                st.toast(f"✅ {n_dl} demo(s) parsed — opening battle plan for {enemy_name}…",
                         icon="⚔️")
                st.session_state["_goto_page"] = "⚔️ Battle plan"
                st.rerun()
            else:
                st.success(f"✅ {n_dl} demo(s) parsed for {enemy_name}.")
        elif links:
            from scout.ingest.faceit import SCOUT_DIR
            st.info(
                f"Found **{len(links)}** {map_label} demo(s) for {enemy_name}. FACEIT only lets "
                "demos download through your **logged-in browser** (the API link isn't directly "
                "downloadable — this is a FACEIT restriction, not your network), so grab them with "
                "two clicks each:"
            )
            st.markdown(
                f"1. **Open each match room** below and click **Download → GOTV demo**.\n"
                f"2. Save the `.dem` / `.dem.zst` files into `{SCOUT_DIR}`.\n"
                f"3. Come back to **📥 Demos → Parse** — compressed demos unpack automatically, "
                f"then **⚔️ Battle plan** is ready."
            )
            for lk in links:
                st.markdown(f"- **{lk['nickname']}** · {lk['map']} — "
                            f"[open match room ↗]({lk['room']})")
            st.caption(f"Demos go in: `{SCOUT_DIR}`  (any subfolder of `{DEMOS_DIR}` also works)")
        else:
            st.warning(f"No {map_label} demos found for {enemy_name} in their recent history. "
                       "Try **Advanced options → any map**, or raise *matches per player*.")


def page_team_maps() -> None:
    st.header("🗺️ Team heatmaps")
    if need_demos():
        return
    fp = _fingerprint()
    ms_all = get_matchset(fp)

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        picked = player_picker(ms_all, key="team_player")
    if not picked:
        return
    steamid, name = picked
    maps = ms_all.player_maps(steamid)
    with c2:
        team_map = st.selectbox("Map", maps, key="team_map")
    with c3:
        side = st.radio("Side", ["T", "CT"], horizontal=True, key="team_side")
    side_num = 2 if side == "T" else 3

    if not has_calibration(team_map):
        st.error(f"No radar calibration for `{team_map}`.")
        return

    ms = ms_all.filtered(team_map)
    mem = team_membership(ms, steamid)
    if mem.empty or ms.deaths.empty:
        st.warning("Not enough data.")
        return
    mates = mem[mem["is_teammate"]][["demo_hash", "round_idx", "steamid"]]

    d = ms.deaths.copy()
    d["attacker_steamid"] = d["attacker_steamid"].astype(str)
    d["user_steamid"] = d["user_steamid"].astype(str)
    kills = d.merge(mates.rename(columns={"steamid": "attacker_steamid"}),
                    on=["demo_hash", "round_idx", "attacker_steamid"])
    kills = kills[kills["attacker_team_num"] == side_num]
    deaths = d.merge(mates.rename(columns={"steamid": "user_steamid"}),
                     on=["demo_hash", "round_idx", "user_steamid"])
    deaths = deaths[deaths["user_team_num"] == side_num]

    st.caption(f"Team of **{name}**, {side} side on {team_map} — "
               f"{kills.shape[0]} kills, {deaths.shape[0]} deaths.")
    cols = st.columns(2)
    with cols[0]:
        st.markdown(f"**{side} kill positions** (whole team)")
        for level in map_levels(team_map):
            part = split_by_level(kills, "attacker_Z", team_map).get(level, kills)
            show_fig(heatmap_figure(part, "attacker_X", "attacker_Y", team_map,
                                    f"team {side} kills", level=level))
    with cols[1]:
        st.markdown(f"**{side} death positions** (whole team)")
        for level in map_levels(team_map):
            part = split_by_level(deaths, "user_Z", team_map).get(level, deaths)
            show_fig(heatmap_figure(part, "user_X", "user_Y", team_map,
                                    f"team {side} deaths", cmap="cool", level=level))

    if not ms.util.empty:
        st.subheader("Utility landing spots (whole team)")
        u = ms.util.merge(mates.rename(columns={"steamid": "thrower_steamid"}),
                          on=["demo_hash", "round_idx", "thrower_steamid"])
        u = u[u["thrower_team_num"] == side_num]
        ucols = st.columns(2)
        with ucols[0]:
            sm = u[u["event"] == "smokegrenade_detonate"]
            show_fig(points_figure([{"df": sm, "x": "x", "y": "y", "color": "#bdc3c7",
                                     "label": "smokes", "size": 60}],
                                   team_map, f"{side} smokes"))
        with ucols[1]:
            mol = u[u["event"] == "inferno_startburn"]
            show_fig(points_figure([{"df": mol, "x": "x", "y": "y", "color": "#e67e22",
                                     "label": "molotovs", "size": 60}],
                                   team_map, f"{side} molotovs"))


# ---------------------------------------------------------------- shell

def _check_password() -> bool:
    """Login gate for the public server. Active only when SCOUT_LOGIN=1.

    Shows a random 6-digit code N; entering floor(N/2) grants a 24h token kept in
    the URL so refreshes stay logged in. Local use (no env) is open.
    """
    if os.environ.get("SCOUT_LOGIN") != "1":
        return True
    if auth.is_valid(st.query_params.get("t")):
        return True
    if "_login_code" not in st.session_state:
        st.session_state["_login_code"] = auth.new_code()
    code = st.session_state["_login_code"]
    st.title("🎯 CS2 Scout — login")
    st.markdown(f"## Code:  `{code}`")
    st.caption("Enter **floor(code ÷ 2)** to get 24-hour access.")
    ans = st.text_input("Your answer", key="_login_ans")
    if st.button("Enter", type="primary"):
        if ans.strip().isdigit() and int(ans) == auth.expected_answer(code):
            st.query_params["t"] = auth.grant()
            del st.session_state["_login_code"]
            st.rerun()
        else:
            st.error("Wrong — here's a new code.")
            st.session_state["_login_code"] = auth.new_code()
    return False


ALL_PAGES = ["👤 Player report", "⚔️ Battle plan", "🔎 Auto-scout", "💣 Team tactics",
             "🗺️ Team heatmaps", "📥 Demos"]
PAGE_FUNCS = {
    "👤 Player report": "page_player", "⚔️ Battle plan": "page_battle",
    "🔎 Auto-scout": "page_autoscout", "💣 Team tactics": "page_tactics",
    "🗺️ Team heatmaps": "page_team_maps", "📥 Demos": "page_demos",
}


def _pages_for_role() -> list[str]:
    """SCOUT_ROLE splits the UI: 'local' = just the scout sender; 'server' = the
    analytics viewer (scouting happens on your PC); unset = everything (all-in-one)."""
    role = os.environ.get("SCOUT_ROLE", "")
    if role == "local":
        return ["🔎 Auto-scout"]
    if role == "server":
        return [p for p in ALL_PAGES if p != "🔎 Auto-scout"]
    return ALL_PAGES


def main() -> None:
    if not _check_password():
        return
    pages = _pages_for_role()
    # honor a programmatic navigation request (set before the radio is created,
    # so it's allowed to drive the widget's value) — e.g. jump to Battle plan post-scout
    pending = st.session_state.pop("_goto_page", None)
    if pending in pages:
        st.session_state["nav_page"] = pending
    elif "nav_page" not in st.session_state:
        # deep link (?view=battle): land on that page on first load
        mapped = {"battle": "⚔️ Battle plan", "player": "👤 Player report",
                  "tactics": "💣 Team tactics", "heatmaps": "🗺️ Team heatmaps",
                  "demos": "📥 Demos", "scout": "🔎 Auto-scout"}.get(st.query_params.get("view"))
        if mapped in pages:
            st.session_state["nav_page"] = mapped
    st.sidebar.title("🎯 CS2 Scout")
    if len(pages) == 1:
        page = pages[0]
    else:
        page = st.sidebar.radio("Pages", pages, key="nav_page", label_visibility="collapsed")
    fp = _fingerprint()
    if fp:
        ms = get_matchset(fp)
        st.sidebar.caption(
            f"**{len(ms.matches)}** demos parsed · maps: {', '.join(ms.maps())}"
        )
    if st.sidebar.button("🔄 Reload data"):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption("All analysis is computed locally from your demos. No AI involved.")

    globals()[PAGE_FUNCS[page]]()


main()

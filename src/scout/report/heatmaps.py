from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ..ingest import Team, discover_demos, load_team
from ..parse import cache_dir, parse_demo
from ..paths import REPORTS_DIR
from ..viz import render_heatmap

T_SIDE = 2
CT_SIDE = 3


def load_deaths(opponent_dir: str | Path, map_filter: str | None = None) -> pd.DataFrame:
    """Parse all demos under opponent_dir and return a stacked deaths DataFrame.

    A 'demo_hash' and 'map_name' column are added so events can be traced to source.
    """
    frames = []
    for demo in discover_demos(opponent_dir):
        result = parse_demo(demo)
        d = cache_dir(result["hash"])
        header = json.loads((d / "header.json").read_text())
        if map_filter and header.get("map_name") != map_filter:
            continue
        deaths = pd.read_parquet(d / "deaths.parquet")
        deaths = deaths.assign(demo_hash=result["hash"], map_name=header.get("map_name"))
        frames.append(deaths)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def render_team_heatmaps(opponent_dir: str | Path, map_name: str) -> dict[str, Path]:
    """Render four kill-position heatmaps for the team labeled in opponent_dir on map_name.

    Returns a dict of {label: png_path}.
    """
    team = load_team(opponent_dir)
    deaths = load_deaths(opponent_dir, map_filter=map_name)
    if deaths.empty:
        raise RuntimeError(f"No demos for {team.name} on {map_name} under {opponent_dir}")

    sids = team.steamids
    deaths["att_is_team"] = deaths["attacker_steamid"].astype(str).isin(sids)
    deaths["vic_is_team"] = deaths["user_steamid"].astype(str).isin(sids)

    out_dir = REPORTS_DIR / team.name / map_name
    out_dir.mkdir(parents=True, exist_ok=True)

    slices = {
        "ct_kills": (
            deaths[deaths["att_is_team"] & ~deaths["vic_is_team"] & (deaths["attacker_team_num"] == CT_SIDE)],
            "attacker_X", "attacker_Y",
            f"{team.name} CT kills on {map_name} (attacker positions)",
        ),
        "ct_deaths": (
            deaths[deaths["vic_is_team"] & ~deaths["att_is_team"] & (deaths["user_team_num"] == CT_SIDE)],
            "user_X", "user_Y",
            f"{team.name} CT deaths on {map_name} (victim positions)",
        ),
        "t_kills": (
            deaths[deaths["att_is_team"] & ~deaths["vic_is_team"] & (deaths["attacker_team_num"] == T_SIDE)],
            "attacker_X", "attacker_Y",
            f"{team.name} T kills on {map_name} (attacker positions)",
        ),
        "t_deaths": (
            deaths[deaths["vic_is_team"] & ~deaths["att_is_team"] & (deaths["user_team_num"] == T_SIDE)],
            "user_X", "user_Y",
            f"{team.name} T deaths on {map_name} (victim positions)",
        ),
    }

    outputs: dict[str, Path] = {}
    for label, (df, x_col, y_col, title) in slices.items():
        outputs[label] = render_heatmap(
            df, x_col=x_col, y_col=y_col,
            map_name=map_name, title=title,
            out_path=out_dir / f"{label}.png",
        )
    return outputs

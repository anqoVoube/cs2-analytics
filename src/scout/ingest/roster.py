from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Team:
    name: str
    steamids: frozenset[str]
    display_names: dict[str, str]

    def contains(self, steamid: str | int) -> bool:
        return str(steamid) in self.steamids

    def display(self, steamid: str | int) -> str:
        return self.display_names.get(str(steamid), str(steamid))


def load_team(opponent_dir: str | Path) -> Team:
    """Load team.json from an opponent folder (e.g. data/demos/DEPO/)."""
    opponent_dir = Path(opponent_dir)
    meta = json.loads((opponent_dir / "team.json").read_text())
    players = meta["players"]
    return Team(
        name=meta["name"],
        steamids=frozenset(str(p["steamid"]) for p in players),
        display_names={str(p["steamid"]): p["name"] for p in players},
    )


def discover_demos(opponent_dir: str | Path) -> list[Path]:
    """Return all .dem files under an opponent folder, recursively."""
    opponent_dir = Path(opponent_dir)
    return sorted(opponent_dir.rglob("*.dem"))

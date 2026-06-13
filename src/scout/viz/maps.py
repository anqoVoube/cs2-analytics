from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests

from ..paths import RADAR_DIR

# CS2 radar calibration. World X/Y in source units; radar PNG assumed 1024x1024.
# pos_x, pos_y = world coords of the radar's top-left pixel; scale = world units per pixel.
# z_cutoff: world Z below which a point belongs on the *_lower.png radar (multi-level maps).
@dataclass(frozen=True)
class MapCalibration:
    pos_x: float
    pos_y: float
    scale: float
    radar_size: int = 1024
    z_cutoff: float | None = None


# Values from the game's overview files (CS2 build ~2025). Override or extend via
# data/radars/map-data.json (awpy format: {"de_x": {"pos_x":..,"pos_y":..,"scale":..}}).
MAP_CALIBRATION: dict[str, MapCalibration] = {
    "de_ancient": MapCalibration(pos_x=-2953, pos_y=2164, scale=5.0),
    "de_anubis": MapCalibration(pos_x=-2796, pos_y=3328, scale=5.22),
    "de_dust2": MapCalibration(pos_x=-2476, pos_y=3239, scale=4.4),
    "de_inferno": MapCalibration(pos_x=-2087, pos_y=3870, scale=4.9),
    "de_mirage": MapCalibration(pos_x=-3230, pos_y=1713, scale=5.0),
    "de_nuke": MapCalibration(pos_x=-3453, pos_y=2887, scale=7.0, z_cutoff=-495),
    "de_overpass": MapCalibration(pos_x=-4831, pos_y=1781, scale=5.2),
    "de_train": MapCalibration(pos_x=-2308, pos_y=2078, scale=4.082077, z_cutoff=-130),
    "de_vertigo": MapCalibration(pos_x=-3168, pos_y=1762, scale=4.0, z_cutoff=11700),
    "cs_italy": MapCalibration(pos_x=-2647, pos_y=2592, scale=4.6),
    "cs_office": MapCalibration(pos_x=-1838, pos_y=1858, scale=4.1),
}


def _load_overrides() -> None:
    """Merge data/radars/map-data.json (awpy format) into MAP_CALIBRATION if present."""
    path = RADAR_DIR / "map-data.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for name, cfg in data.items():
        try:
            MAP_CALIBRATION[name] = MapCalibration(
                pos_x=float(cfg["pos_x"]),
                pos_y=float(cfg["pos_y"]),
                scale=float(cfg["scale"]),
                z_cutoff=cfg.get("lower_level_max_units"),
            )
        except (KeyError, TypeError, ValueError):
            continue


_load_overrides()


def has_calibration(map_name: str) -> bool:
    return map_name in MAP_CALIBRATION


# Known sources for radar PNGs. We try in order; first 200 wins.
# Steam build IDs that awpycs.com hosts radar archives for. Newest first.
_AWPY_BUILD_IDS = (17595823,)


def _radars_zip_urls() -> list[str]:
    return [f"https://awpycs.com/{bid}/maps.zip" for bid in _AWPY_BUILD_IDS]


def _download_and_extract_radars() -> list[Path]:
    """Try each known awpy radar archive URL until one works; extract all PNGs into RADAR_DIR."""
    RADAR_DIR.mkdir(parents=True, exist_ok=True)
    errors = []
    for url in _radars_zip_urls():
        try:
            r = requests.get(url, timeout=30)
        except requests.RequestException as e:
            errors.append(f"{type(e).__name__} {url}")
            continue
        if r.status_code != 200:
            errors.append(f"{r.status_code} {url}")
            continue
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            extracted: list[Path] = []
            for member in zf.namelist():
                if not member.lower().endswith(".png"):
                    continue
                target = RADAR_DIR / Path(member).name
                target.write_bytes(zf.read(member))
                extracted.append(target)
            return extracted
    raise FileNotFoundError(
        "Could not download radar archive. Tried:\n  "
        + "\n  ".join(errors)
        + f"\nDrop radar PNGs into: {RADAR_DIR}"
    )


def world_to_pixel(world_x, world_y, map_name: str) -> tuple[np.ndarray, np.ndarray]:
    """Convert world coords (arrays or scalars) to radar pixel coords."""
    cal = MAP_CALIBRATION[map_name]
    px = (np.asarray(world_x) - cal.pos_x) / cal.scale
    py = (cal.pos_y - np.asarray(world_y)) / cal.scale
    return px, py


def radar_path(map_name: str, level: str = "upper") -> Path:
    suffix = "_lower" if level == "lower" else ""
    return RADAR_DIR / f"{map_name}{suffix}.png"


def map_levels(map_name: str) -> list[str]:
    """Radar levels available for a map: ['upper'] or ['upper', 'lower']."""
    cal = MAP_CALIBRATION.get(map_name)
    if cal and cal.z_cutoff is not None and radar_path(map_name, "lower").exists():
        return ["upper", "lower"]
    return ["upper"]


def ensure_radar(map_name: str, level: str = "upper") -> Path:
    """Return a local path to the radar PNG, downloading the awpy archive if missing."""
    out = radar_path(map_name, level)
    if out.exists():
        return out
    _download_and_extract_radars()
    if out.exists():
        return out
    raise FileNotFoundError(
        f"Radar archive downloaded but {out.name} not found inside. "
        f"Check {RADAR_DIR} for the actual filename."
    )

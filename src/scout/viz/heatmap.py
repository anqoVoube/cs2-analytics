from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image
from scipy.ndimage import gaussian_filter

from .maps import MAP_CALIBRATION, ensure_radar, map_levels, world_to_pixel


def split_by_level(df: pd.DataFrame, z_col: str, map_name: str) -> dict[str, pd.DataFrame]:
    """Split points into radar levels for multi-level maps (nuke/vertigo/train)."""
    levels = map_levels(map_name)
    if "lower" not in levels or z_col not in df.columns:
        return {"upper": df}
    cutoff = MAP_CALIBRATION[map_name].z_cutoff
    z = pd.to_numeric(df[z_col], errors="coerce")
    return {"upper": df[z >= cutoff], "lower": df[z < cutoff]}


def base_figure(map_name: str, level: str = "upper", figsize: float = 7.0):
    """Radar background figure; returns (fig, ax, size)."""
    cal = MAP_CALIBRATION[map_name]
    radar = np.asarray(Image.open(ensure_radar(map_name, level)).convert("RGB"))
    size = cal.radar_size
    fig, ax = plt.subplots(figsize=(figsize, figsize), dpi=110)
    ax.imshow(radar, extent=(0, size, size, 0))
    ax.set_xlim(0, size)
    ax.set_ylim(size, 0)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.patch.set_facecolor("#0e1117")
    return fig, ax, size


def _to_pixels(points: pd.DataFrame, x_col: str, y_col: str, map_name: str, size: int):
    px, py = world_to_pixel(points[x_col].to_numpy(), points[y_col].to_numpy(), map_name)
    in_bounds = (px >= 0) & (px < size) & (py >= 0) & (py < size)
    return px[in_bounds], py[in_bounds]


def heatmap_figure(
    points: pd.DataFrame,
    x_col: str,
    y_col: str,
    map_name: str,
    title: str,
    sigma: float = 14.0,
    cmap: str = "inferno",
    alpha: float = 0.65,
    level: str = "upper",
):
    """Density heatmap of world-coordinate points over the radar. Returns a matplotlib figure."""
    fig, ax, size = base_figure(map_name, level)
    px, py = _to_pixels(points, x_col, y_col, map_name, size)

    if len(px):
        h, _, _ = np.histogram2d(py, px, bins=size, range=[[0, size], [0, size]])
        grid = gaussian_filter(h.astype(np.float32), sigma=sigma)
        if grid.max() > 0:
            grid = grid / grid.max()
            masked = np.ma.masked_less(grid, 0.02)
            ax.imshow(masked, extent=(0, size, size, 0), cmap=cmap, alpha=alpha, origin="upper")
    ax.set_title(f"{title}  (n={len(px)})", color="white")
    fig.tight_layout()
    return fig


def points_figure(
    layers: list[dict],
    map_name: str,
    title: str,
    level: str = "upper",
):
    """Scatter layers over the radar. Each layer: {df, x, y, color, label, marker?, size?}."""
    fig, ax, size = base_figure(map_name, level)
    total = 0
    for layer in layers:
        df = layer["df"]
        if df is None or len(df) == 0:
            continue
        px, py = _to_pixels(df, layer["x"], layer["y"], map_name, size)
        total += len(px)
        ax.scatter(
            px,
            py,
            s=layer.get("size", 28),
            c=layer.get("color", "red"),
            marker=layer.get("marker", "o"),
            alpha=0.75,
            edgecolors="black",
            linewidths=0.4,
            label=f"{layer.get('label', '')} ({len(px)})",
        )
    if total:
        ax.legend(loc="lower right", fontsize=8, framealpha=0.6)
    ax.set_title(f"{title}  (n={total})", color="white")
    fig.tight_layout()
    return fig


def paths_figure(
    paths: list[dict],
    map_name: str,
    title: str,
    level: str = "upper",
):
    """Movement trajectories over the radar. Each path: {x: array, y: array, label, color?}.

    Start of each path is marked with a dot, end with an X.
    """
    fig, ax, size = base_figure(map_name, level)
    cmap = plt.get_cmap("tab10")
    drawn = 0
    for i, path in enumerate(paths):
        x, y = np.asarray(path["x"], dtype=float), np.asarray(path["y"], dtype=float)
        if len(x) < 2:
            continue
        px, py = world_to_pixel(x, y, map_name)
        color = path.get("color", cmap(i % 10))
        ax.plot(px, py, color=color, linewidth=1.6, alpha=0.85, label=path.get("label"))
        ax.scatter([px[0]], [py[0]], s=42, c=[color], marker="o", zorder=5,
                   edgecolors="white", linewidths=0.8)
        ax.scatter([px[-1]], [py[-1]], s=60, c=[color], marker="X", zorder=5,
                   edgecolors="white", linewidths=0.8)
        drawn += 1
    if drawn and drawn <= 12:
        ax.legend(loc="lower right", fontsize=8, framealpha=0.6)
    ax.set_title(f"{title}  ({drawn} rounds)", color="white")
    fig.tight_layout()
    return fig


def render_heatmap(
    points: pd.DataFrame,
    x_col: str,
    y_col: str,
    map_name: str,
    title: str,
    out_path: str | Path,
    sigma: float = 14.0,
    cmap: str = "inferno",
    alpha: float = 0.65,
) -> Path:
    """Render a heatmap PNG to disk (file-based variant of heatmap_figure)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig = heatmap_figure(points, x_col, y_col, map_name, title, sigma=sigma, cmap=cmap, alpha=alpha)
    fig.savefig(out_path, bbox_inches="tight", facecolor="black")
    plt.close(fig)
    return out_path

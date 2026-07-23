"""Split frames into overlapping tiles and merge detections."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from streamer.wildlife.types import Detection


@dataclass(frozen=True)
class Tile:
    """One crop of a full frame plus its pixel origin/size."""

    rgb: np.ndarray
    x0: int
    y0: int
    width: int
    height: int


def _axis_origins(length: int, count: int, overlap: float) -> list[tuple[int, int]]:
    """Return (origin, size) pairs covering ``length`` with ``count`` windows."""

    if count < 1:
        raise ValueError("tile count must be >= 1")
    if length < 1:
        raise ValueError("axis length must be >= 1")
    if count == 1:
        return [(0, length)]

    # Window size such that consecutive windows overlap by ``overlap``.
    # length = size + (count - 1) * size * (1 - overlap)
    size = int(round(length / (1.0 + (count - 1) * (1.0 - overlap))))
    size = max(1, min(length, size))
    if count == 1:
        return [(0, size)]
    span = length - size
    origins: list[tuple[int, int]] = []
    for i in range(count):
        x0 = int(round(i * span / (count - 1))) if span > 0 else 0
        x0 = max(0, min(x0, length - size))
        origins.append((x0, size))
    # Ensure the last window always reaches the far edge.
    last_x0, last_size = origins[-1]
    if last_x0 + last_size < length:
        origins[-1] = (length - last_size, last_size)
    return origins


def iter_tiles(
    rgb: np.ndarray,
    grid: tuple[int, int],
    tile_size: tuple[int, int],
    overlap: float,
) -> list[Tile]:
    """Split ``rgb`` (H, W, 3) into a ``cols x rows`` overlapping grid.

    Each region is resized by the detector to ``tile_size`` / inference
    size. Region geometry is chosen so the grid covers the full frame
    with the requested fractional overlap between neighbors.
    """

    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected HxWx3 RGB array, got shape {rgb.shape}")
    height, width = rgb.shape[0], rgb.shape[1]
    cols, rows = grid
    if cols < 1 or rows < 1:
        raise ValueError(f"tile_grid must be positive, got {grid}")
    del tile_size  # Detector resizes; region size comes from coverage math.

    x_windows = _axis_origins(width, cols, overlap)
    y_windows = _axis_origins(height, rows, overlap)
    tiles: list[Tile] = []
    for y0, th in y_windows:
        for x0, tw in x_windows:
            crop = rgb[y0 : y0 + th, x0 : x0 + tw].copy()
            tiles.append(Tile(rgb=crop, x0=x0, y0=y0, width=tw, height=th))
    return tiles


def remap_detection(
    detection: Detection, tile: Tile, full_width: int, full_height: int
) -> Detection:
    """Map a tile-local normalized box onto the full frame."""

    if full_width < 1 or full_height < 1:
        raise ValueError("full frame dimensions must be >= 1")
    abs_xc = tile.x0 + detection.x_center * tile.width
    abs_yc = tile.y0 + detection.y_center * tile.height
    abs_w = detection.width * tile.width
    abs_h = detection.height * tile.height
    return Detection(
        species=detection.species,
        confidence=detection.confidence,
        x_center=abs_xc / full_width,
        y_center=abs_yc / full_height,
        width=abs_w / full_width,
        height=abs_h / full_height,
    )


def _iou(a: Detection, b: Detection) -> float:
    ax0 = a.x_center - a.width / 2
    ay0 = a.y_center - a.height / 2
    ax1 = a.x_center + a.width / 2
    ay1 = a.y_center + a.height / 2
    bx0 = b.x_center - b.width / 2
    by0 = b.y_center - b.height / 2
    bx1 = b.x_center + b.width / 2
    by1 = b.y_center + b.height / 2

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0.0, ix1 - ix0)
    ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    union = a.width * a.height + b.width * b.height - inter
    if union <= 0.0:
        return 0.0
    return inter / union


def nms_merge(
    detections: list[Detection], *, iou_threshold: float = 0.5
) -> list[Detection]:
    """Greedy per-class NMS; keeps highest-confidence boxes."""

    if not detections:
        return []
    by_class: dict[str, list[Detection]] = {}
    for det in detections:
        by_class.setdefault(det.species, []).append(det)

    kept: list[Detection] = []
    for group in by_class.values():
        ordered = sorted(group, key=lambda d: d.confidence, reverse=True)
        while ordered:
            best = ordered.pop(0)
            kept.append(best)
            ordered = [d for d in ordered if _iou(best, d) < iou_threshold]
    kept.sort(key=lambda d: d.confidence, reverse=True)
    return kept

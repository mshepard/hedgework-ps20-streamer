"""Save annotated detection images to disk."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from streamer.wildlife.types import Detection


def _box_pixels(
    detection: Detection, width: int, height: int
) -> tuple[int, int, int, int]:
    x_center = detection.x_center * width
    y_center = detection.y_center * height
    box_w = detection.width * width
    box_h = detection.height * height
    x0 = int(max(0, x_center - box_w / 2))
    y0 = int(max(0, y_center - box_h / 2))
    x1 = int(min(width, x_center + box_w / 2))
    y1 = int(min(height, y_center + box_h / 2))
    return x0, y0, x1, y1


def annotate_frame(
    rgb: np.ndarray,
    detection: Detection,
    *,
    store_annotated: bool,
) -> Image.Image:
    image = Image.fromarray(rgb, mode="RGB")
    if not store_annotated:
        return image

    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = _box_pixels(detection, image.width, image.height)
    draw.rectangle([x0, y0, x1, y1], outline="#ff8c44", width=3)
    label = f"{detection.display_name} {detection.confidence:.0%}"
    text_y = max(0, y0 - 18)
    draw.rectangle([x0, text_y, x0 + 8 * len(label), text_y + 18], fill="#15312b")
    draw.text((x0 + 4, text_y + 2), label, fill="#FAFBFC")
    return image


def save_detection_image(
    base_dir: Path,
    camera_num: int,
    detection: Detection,
    rgb: np.ndarray,
    *,
    store_annotated: bool,
    detected_at: datetime | None = None,
) -> Path:
    when = detected_at or datetime.now(timezone.utc)
    day_dir = base_dir / when.strftime("%Y-%m-%d") / f"cam{camera_num}"
    day_dir.mkdir(parents=True, exist_ok=True)
    stamp = when.strftime("%H%M%S")
    safe_species = detection.species.replace(" ", "_")
    filename = f"{stamp}_{safe_species}_{detection.confidence:.2f}.jpg"
    path = day_dir / filename

    image = annotate_frame(rgb, detection, store_annotated=store_annotated)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    path.write_bytes(buf.getvalue())
    return path


def resize_for_upload(path: Path, max_width: int) -> bytes:
    with Image.open(path) as image:
        if image.width <= max_width:
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
        ratio = max_width / image.width
        resized = image.resize(
            (max_width, int(image.height * ratio)), Image.Resampling.LANCZOS
        )
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=80)
        return buf.getvalue()

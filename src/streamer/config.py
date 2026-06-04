"""Configuration loader.

TOML file -> pydantic settings model. The Phase 1 surface is intentionally
narrow; Phase 2 will expand ``PowerConfig`` and add ``[schedule]``,
``[location]``, and ``[network]`` blocks for solar-aware sleep cycles.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


DEFAULT_CONFIG_PATHS = (
    Path("/etc/streamer/streamer.toml"),
    Path("config/streamer.toml"),
    Path(__file__).resolve().parent.parent.parent / "config" / "streamer.toml",
)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    # Single bearer token. Required on every API and stream call, either as
    # an ``Authorization: Bearer <token>`` header or a ``?key=<token>``
    # query parameter. The installer rotates the placeholder on first
    # install; leaving the literal "change-me" in place is logged loudly
    # at startup.
    auth_token: str = "change-me"
    # Brand label shown on the per-camera viewer pages and in the browser
    # tab title. Empty falls back to "Streamer".
    site_name: str = "HEDGEWORK @ PS 20"


class CameraConfig(BaseModel):
    resolution: tuple[int, int] = (1280, 720)
    # picamera2 sensor controls passed straight through (e.g. AwbMode,
    # ExposureValue). Keep empty unless tuning a specific scene.
    controls: dict[str, Any] = Field(default_factory=dict)
    # Optional human-friendly label shown on the public /camN page header
    # (e.g. "Pasture View"). Falls back to "Camera N" when empty.
    name: str = ""

    @field_validator("resolution", mode="before")
    @classmethod
    def _coerce_resolution(cls, v: Any) -> tuple[int, int]:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return (int(v[0]), int(v[1]))
        raise ValueError("resolution must be [width, height]")


class StreamConfig(BaseModel):
    # MJPEG output framerate. The sensor is configured with matching
    # ``FrameDurationLimits`` so producer and consumer run at the same
    # rate (no slow-consumer buffer pressure). 1.0 is a good balance of
    # LTE bandwidth and a live-looking feed.
    framerate: float = Field(default=1.0, ge=0.25, le=15.0)
    # JPEG quality 1-95. Higher = bigger file. 75 is a reasonable balance.
    jpeg_quality: int = Field(default=75, ge=1, le=95)


class PowerConfig(BaseModel):
    """Phase 1 power settings. Phase 2 expands this block significantly."""

    disable_act_led: bool = True
    # Seconds a camera with refcount=0 lingers before its picamera2
    # instance is closed. Short delay keeps fast reconnects cheap (no
    # ~150 ms re-init); long enough doesn't matter much because the
    # consumer pattern is steady-state streaming.
    idle_grace_seconds: int = Field(default=10, ge=0)
    # Hold both cameras open at refcount=1 from service start.
    # At low framerates (≤2 fps) picamera2's first frame after a cold
    # ``start()`` can take 3-10 s — long enough to make viewer-side
    # cold starts feel broken. Keeping both pipelines warm continuously
    # eliminates that delay; the cost is ~1 W extra at idle (two IMX708
    # sensors active at 1 fps). Phase 2's schedule layer is expected to
    # flip this off outside the active window so the cost is only paid
    # while the system is supposed to be serving frames anyway.
    keep_cameras_warm: bool = True


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    camera0: CameraConfig = Field(default_factory=CameraConfig)
    camera1: CameraConfig = Field(default_factory=CameraConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)

    @classmethod
    def from_toml(cls, path: Path) -> AppConfig:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.model_validate(data)


def _candidate_paths(explicit: Path | None) -> list[Path]:
    if explicit:
        return [explicit]
    env = os.environ.get("STREAMER_CONFIG")
    paths: list[Path] = [Path(env)] if env else []
    paths.extend(DEFAULT_CONFIG_PATHS)
    return paths


def load_config(explicit_path: Path | None = None) -> AppConfig:
    """Load config from the first matching path on disk, or defaults."""

    for candidate in _candidate_paths(explicit_path):
        if candidate.is_file():
            return AppConfig.from_toml(candidate)
    return AppConfig()

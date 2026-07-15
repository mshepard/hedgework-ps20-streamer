"""Configuration loader.

TOML file -> pydantic settings model. Phase 2 added the ``[location]``,
``[schedule]``, and ``[network]`` blocks plus the ``power.dry_run`` flag
for the solar-aware sleep cycle. ``[schedule].enabled`` is the master
switch — when ``false`` (the default), the rest of the schedule block
and ``[location]`` are not required, and the service behaves exactly
like Phase 1.
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
    # When true, ``/stream/cam0``, ``/stream/cam1``, and a new
    # ``/api/public/status`` endpoint are reachable anonymously (no
    # token, with permissive CORS headers) so they can be embedded in
    # a third-party webpage (e.g. a WordPress page hosted elsewhere).
    # The token-protected ``/api/status``, ``/api/info``, and
    # ``/api/admin/*`` endpoints stay locked regardless. This is a
    # public-internet opt-in: only enable when the intent is for
    # anyone-with-the-URL to watch the streams.
    public_streams: bool = False


class CameraWildlifeConfig(BaseModel):
    # Optional per-camera model override (.hef on Pi).
    model_path: str = ""
    labels_path: str = ""
    # Empty = accept all classes from the model on this camera.
    classes: list[str] = Field(default_factory=list)


class CameraConfig(BaseModel):
    resolution: tuple[int, int] = (1280, 720)
    # picamera2 sensor controls passed straight through (e.g. AwbMode,
    # ExposureValue). Keep empty unless tuning a specific scene.
    controls: dict[str, Any] = Field(default_factory=dict)
    # Optional human-friendly label shown on the public /camN page header
    # (e.g. "Pasture View"). Falls back to "Camera N" when empty.
    name: str = ""
    wildlife: CameraWildlifeConfig = Field(default_factory=lambda: CameraWildlifeConfig())

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
    # Close each MJPEG connection after this many seconds. Prevents a
    # forgotten browser tab from holding a stream (and LTE bandwidth)
    # indefinitely. 0 disables the limit.
    max_duration_seconds: int = Field(default=3600, ge=0)


class PowerConfig(BaseModel):
    """Power settings.

    Phase 2 added ``dry_run``: when true, the state machine still
    transitions through ``ENTERING_SLEEP`` -> ``ASLEEP`` on the
    configured schedule but never actually calls ``rtcwake`` /
    ``systemctl poweroff``. ``/stream/*`` returns ``503 SLEEPING``
    during the ASLEEP window instead. Useful for rehearsing a sleep
    cycle on a bench Pi without halting it.
    """

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
    # sensors active at 1 fps). The Phase 2 power state machine
    # automatically releases these refcounts outside the active window
    # so the cost is only paid while the system is supposed to be
    # serving frames anyway.
    keep_cameras_warm: bool = True
    dry_run: bool = False
    # ---- Self power-cycle recovery (last-resort camera rescue) ----
    # Field experience: a marginal camera link (THSER102A SerDes) can
    # wedge in a way that survives service restarts and even warm
    # reboots — only a genuine power cut re-trains it. With
    # POWER_OFF_ON_HALT=1 in the EEPROM, ``rtcwake + poweroff`` IS a
    # power cut, so the service can rescue itself: after a camera
    # fails ``recovery_failure_threshold`` consecutive in-stream
    # recovery attempts, arm the RTC for ``recovery_wake_delay_seconds``
    # in the future and power off. Total outage ≈ 2-3 minutes.
    # Guardrails: at most ``recovery_max_cycles_per_day`` self-cycles
    # per calendar day; never within ``recovery_boot_grace_minutes``
    # of service start (prevents a dead camera boot-looping the Pi);
    # never while ENTERING_SLEEP/ASLEEP (the sunset path owns power
    # there). ``power.dry_run = true`` logs the decision but skips
    # the actual power-off.
    recovery_power_cycle: bool = False
    recovery_failure_threshold: int = Field(default=5, ge=2)
    recovery_max_cycles_per_day: int = Field(default=3, ge=1)
    recovery_boot_grace_minutes: int = Field(default=10, ge=1)
    recovery_wake_delay_seconds: int = Field(default=120, ge=60)


class LocationConfig(BaseModel):
    """Geographic location used by the astral schedule.

    Only consulted when ``schedule.enabled = true``. Values are not
    required otherwise — Phase 1 deployments that don't enable the
    schedule can leave this block at its defaults.
    """

    latitude: float = Field(default=0.0, ge=-90.0, le=90.0)
    longitude: float = Field(default=0.0, ge=-180.0, le=180.0)
    # IANA timezone name (``America/New_York``, ``Europe/Berlin``, …).
    # ``UTC`` is a safe fallback; the schedule will still compute
    # correctly but operators will read the timestamps in UTC.
    timezone: str = "UTC"


class ScheduleConfig(BaseModel):
    """Astral-based AWAKE/ASLEEP schedule.

    Active window per civil day: ``[sunrise + sunrise_offset_minutes,
    sunset + sunset_offset_minutes]``. Outside that range the power
    state machine transitions to ``ENTERING_SLEEP`` at ``sunset -
    warn_minutes_before_sleep`` (UX lead-time for viewers), then to
    ``ASLEEP`` at sunset, where the Pi is powered off with an RTC
    alarm set for ``next_sunrise - wake_lead_minutes``.

    ``enabled = false`` (the default) keeps the service in the Phase 1
    "always AWAKE" regime — neither this block nor ``[location]``
    needs to be populated.
    """

    enabled: bool = False
    sunrise_offset_minutes: int = 0
    sunset_offset_minutes: int = 0
    warn_minutes_before_sleep: int = Field(default=15, ge=0)
    wake_lead_minutes: int = Field(default=5, ge=0)


class NetworkConfig(BaseModel):
    """LTE / uplink health probe.

    A background task pings ``modem_probe_target`` every
    ``modem_probe_interval_seconds`` and surfaces the last result on
    ``/api/status``. No automatic remediation — diagnosis only.
    """

    modem_probe_target: str = "1.1.1.1"
    modem_probe_interval_seconds: int = Field(default=60, ge=5)
    modem_probe_timeout_seconds: int = Field(default=5, ge=1)


class WildlifeSyncConfig(BaseModel):
    enabled: bool = False
    wordpress_url: str = "https://ps20.hedgework.net"
    wordpress_user: str = ""
    wordpress_app_password: str = ""
    batch_size: int = Field(default=5, ge=1)
    max_uploads_per_hour: int = Field(default=30, ge=1)
    resize_width: int = Field(default=800, ge=320)


class WildlifeConfig(BaseModel):
    enabled: bool = False
    model_path: str = "/var/lib/streamer/models/wildlife.hef"
    labels_path: str = "/var/lib/streamer/models/wildlife.json"
    confidence_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    cooldown_seconds: float = Field(default=30.0, ge=0.0)
    inference_size: tuple[int, int] = (640, 640)
    store_annotated: bool = True
    sync: WildlifeSyncConfig = Field(default_factory=WildlifeSyncConfig)

    @field_validator("inference_size", mode="before")
    @classmethod
    def _coerce_inference_size(cls, v: Any) -> tuple[int, int]:
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return (int(v[0]), int(v[1]))
        raise ValueError("inference_size must be [width, height]")


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    camera0: CameraConfig = Field(default_factory=CameraConfig)
    camera1: CameraConfig = Field(default_factory=CameraConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    wildlife: WildlifeConfig = Field(default_factory=WildlifeConfig)

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

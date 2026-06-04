"""Entrypoint: ``python -m streamer`` or the installed ``streamer`` script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from streamer import __version__
from streamer.cameras import CameraManager, build_manager
from streamer.config import load_config
from streamer.server import StreamerServer


# How long the warm-up pass waits for the first frame from each camera.
# Generous because at 1 fps the first frame can take 1.5-2.5 s, and
# the whole point of this warm-up is to take the pain at boot instead
# of pushing it onto the first viewer.
WARMUP_TIMEOUT_SECONDS = 5.0


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _apply_phase1_power_hooks(disable_act_led: bool) -> None:
    """One-shot OS tweaks for Phase 1.

    The activity LED trigger lives in /sys and requires root to write.
    The service runs as the unprivileged ``streamer`` user, so this
    best-effort attempt will normally log a debug message rather than
    succeed. Phase 2 will wire up the privileged power.py path that can
    drive LEDs, the CPU governor, and HARD_SLEEP via ``rtcwake``.
    """

    if not disable_act_led:
        return
    led_trigger = Path("/sys/class/leds/ACT/trigger")
    if not led_trigger.exists():
        return
    log = logging.getLogger("streamer.power")
    try:
        led_trigger.write_text("none\n")
        log.info("Activity LED disabled")
    except OSError as ex:
        log.debug(
            "Could not disable ACT LED (expected as non-root in Phase 1): %s", ex
        )


async def _warmup_cameras(cameras: CameraManager, log: logging.Logger) -> None:
    """Acquire each camera in turn, capture one frame, release.

    Sequential rather than concurrent so the boot log is easy to read
    and so libcamera doesn't try to allocate both pipelines in the
    exact same instant (which itself has been flaky on Pi 5 dual-cam
    setups). After release, each camera will idle-stop after
    ``power.idle_grace_seconds`` — long enough that the next viewer
    connection is unlikely to require a full cold start, but short
    enough that we're not burning camera power forever.
    """

    for cam in cameras.all():
        try:
            async with cam.session():
                await asyncio.wait_for(
                    cam.capture(), timeout=WARMUP_TIMEOUT_SECONDS
                )
            log.info("Camera %d warmed", cam.camera_num)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning(
                "Camera %d warmup timed out after %.1fs; "
                "first-viewer recovery path will handle it",
                cam.camera_num,
                WARMUP_TIMEOUT_SECONDS,
            )
            # Don't mark broken here — the recovery path on the first
            # real viewer will do it if needed.
        except Exception:
            log.exception("Camera %d warmup failed", cam.camera_num)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="streamer")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to streamer.toml "
        "(default: /etc/streamer/streamer.toml or ./config/streamer.toml)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="enable debug logging"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _configure_logging(args.verbose)
    log = logging.getLogger("streamer.main")
    log.info("Streamer %s starting up", __version__)

    config = load_config(args.config)
    log.info(
        "Config: server=%s:%d, cam0=%dx%d, cam1=%dx%d, stream=%.2ffps q%d",
        config.server.host,
        config.server.port,
        config.camera0.resolution[0],
        config.camera0.resolution[1],
        config.camera1.resolution[0],
        config.camera1.resolution[1],
        config.stream.framerate,
        config.stream.jpeg_quality,
    )
    if config.server.auth_token in ("", "change-me"):
        log.warning(
            "Server auth_token is the default/empty value; set a real token "
            "in config before any non-bench deployment"
        )

    _apply_phase1_power_hooks(config.power.disable_act_led)

    cameras = build_manager(config)
    server = StreamerServer(config, cameras)
    app = server.build()

    async def _warmup_pipelines(_app: web.Application) -> None:
        """Briefly touch each camera so libcamera/PiSP allocates both
        halves of the dual-IMX708 pipeline before any viewer connects.

        Empirically, opening one camera on a Pi 5 dual-IMX708 setup
        from a fully cold state often fails to deliver frames at all
        until the other camera is also started — the PiSP scheduler
        appears to coordinate the two pipelines and needs both ends
        active. Doing one acquire/capture/release per camera at boot
        primes that state, after which single-camera streams work.

        Best-effort: a failure here logs and moves on; the regular
        capture-timeout recovery path picks up the slack later.
        """

        log = logging.getLogger("streamer.warmup")
        log.info("Warming up camera pipelines")
        await _warmup_cameras(cameras, log)
        log.info("Warmup complete")

    async def _on_cleanup(_app: web.Application) -> None:
        await cameras.shutdown()

    app.on_startup.append(_warmup_pipelines)
    app.on_cleanup.append(_on_cleanup)

    web.run_app(
        app,
        host=config.server.host,
        port=config.server.port,
        access_log=None,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

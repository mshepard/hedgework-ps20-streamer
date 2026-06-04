"""Entrypoint: ``python -m streamer`` or the installed ``streamer`` script."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from streamer import __version__
from streamer.cameras import Camera, CameraManager, build_manager
from streamer.config import AppConfig, load_config
from streamer.server import StreamerServer


# How long the warm-up pass waits for the first frame from each camera.
# At low framerates (≤2 fps) picamera2's first frame after a cold
# ``start()`` can take 3-10 s — it has to wait for one full
# ``FrameDurationLimits`` interval *and* for libcamera's IPA/3A
# (AGC/AWB) to converge across several frames. 15 s is comfortably
# above the worst case we've observed at 1 fps.
WARMUP_TIMEOUT_SECONDS = 15.0


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


async def _warmup_cameras(
    cameras: CameraManager, config: AppConfig, log: logging.Logger
) -> None:
    """Start both cameras concurrently and wait for the first frame.

    Concurrent rather than sequential: starting both picamera2 instances
    at the same instant gives libcamera/PiSP the best chance to
    coordinate both halves of the dual-IMX708 pipeline together — the
    regime the user-facing streams will mostly run in. Starting them
    one at a time has been observed to leave the second one in a
    state where its first frame takes >5 s to deliver.

    If ``power.keep_cameras_warm`` is set (default), each camera is
    left at refcount=1 so it stays running for the lifetime of the
    service. Viewers then never see a cold-start delay. Otherwise the
    warm-up acquires are released and each camera follows the usual
    ``idle_grace_seconds`` idle-stop path.
    """

    hold = config.power.keep_cameras_warm

    async def _warm_one(cam: Camera) -> None:
        try:
            await cam.acquire()
        except Exception:
            log.exception(
                "Camera %d acquire failed during warmup", cam.camera_num
            )
            return
        warmed = False
        try:
            await asyncio.wait_for(
                cam.capture(), timeout=WARMUP_TIMEOUT_SECONDS
            )
            warmed = True
            log.info("Camera %d warmed", cam.camera_num)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning(
                "Camera %d warmup timed out after %.1fs; "
                "stream recovery path will retry",
                cam.camera_num,
                WARMUP_TIMEOUT_SECONDS,
            )
            cam.mark_broken()
        except Exception:
            log.exception("Camera %d warmup failed", cam.camera_num)
            cam.mark_broken()
        finally:
            # Release unless we're meant to hold the pipeline warm,
            # AND in that case only hold cameras that actually warmed
            # successfully — a broken instance left at refcount=1
            # would just spin in recovery forever with nothing useful
            # to recover into. Releasing it lets the next viewer's
            # acquire rebuild it cleanly.
            if not hold or not warmed:
                try:
                    await cam.release()
                except Exception:
                    log.exception(
                        "Release after warmup failed for camera %d",
                        cam.camera_num,
                    )

    await asyncio.gather(*(_warm_one(cam) for cam in cameras.all()))


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
        """Start both cameras at service boot and (optionally) keep
        them running so viewer connects never pay the cold-start cost.

        Best-effort: a per-camera failure here is logged and won't
        stop the service; the regular capture-timeout recovery path
        picks up the slack on the next viewer connect.
        """

        log = logging.getLogger("streamer.warmup")
        mode = "hold" if config.power.keep_cameras_warm else "touch"
        log.info("Warming up camera pipelines (concurrent, mode=%s)", mode)
        await _warmup_cameras(cameras, config, log)
        if config.power.keep_cameras_warm:
            log.info(
                "Warmup complete; holding cameras warm "
                "(power.keep_cameras_warm = true)"
            )
        else:
            log.info(
                "Warmup complete; cameras released "
                "(idle stop in %ds)",
                config.power.idle_grace_seconds,
            )

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

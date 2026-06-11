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
from streamer.modem import ModemProbe
from streamer.power import PowerManager
from streamer.server import StreamerServer


# Where the sleep_enabled override + (future) power state lives.
# Created by the installer with mode 0750, owner streamer:streamer.
# Falls back to a per-user dir in the venv when running from a checkout
# (developer bench mode) so we don't need write access to /var.
DEFAULT_STATE_DIR = Path("/var/lib/streamer")
DEV_STATE_DIR = Path("/tmp/streamer-state")


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
) -> list[Camera]:
    """Start both cameras concurrently and wait for the first frame.

    Concurrent rather than sequential: starting both picamera2 instances
    at the same instant gives libcamera/PiSP the best chance to
    coordinate both halves of the dual-IMX708 pipeline together — the
    regime the user-facing streams will mostly run in. Starting them
    one at a time has been observed to leave the second one in a
    state where its first frame takes >5 s to deliver.

    Returns the list of cameras whose warmup succeeded *and* are being
    held at refcount=1 by this function. The Phase 2 ``PowerManager``
    takes ownership of those refcounts via ``apply_initial_hold`` so
    it can release them when the schedule transitions out of AWAKE.
    """

    hold = config.power.keep_cameras_warm
    held: list[Camera] = []

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
            else:
                held.append(cam)

    await asyncio.gather(*(_warm_one(cam) for cam in cameras.all()))
    return held


def _resolve_state_dir(log: logging.Logger) -> Path:
    """Pick a writable directory for the sleep-override file.

    Prefers the installer-managed ``/var/lib/streamer``. Falls back
    to a /tmp directory when running from a checkout without root —
    this means a bench Pi or developer laptop doesn't crash trying
    to persist state, at the cost of losing the override across
    reboots in that environment.
    """

    if DEFAULT_STATE_DIR.is_dir():
        return DEFAULT_STATE_DIR
    log.warning(
        "%s missing; using developer fallback %s for sleep-override state",
        DEFAULT_STATE_DIR,
        DEV_STATE_DIR,
    )
    DEV_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return DEV_STATE_DIR


def _validate_schedule_config(config: AppConfig, log: logging.Logger) -> bool:
    """Returns True if the schedule should run, False if it must be disabled.

    Mutates nothing; the caller decides what to do. We refuse to run
    the schedule when ``[location]`` is still at default values
    because that would compute sunrise/sunset for (0, 0) — possibly
    putting the active window in the wrong half of the day.
    """

    if not config.schedule.enabled:
        return False
    lat = config.location.latitude
    lon = config.location.longitude
    if lat == 0.0 and lon == 0.0:
        log.error(
            "[schedule].enabled = true but [location] is at its default "
            "(latitude=0, longitude=0). Set real coordinates in "
            "streamer.toml. Schedule will run as DISABLED until fixed."
        )
        return False
    return True


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

    # If the schedule is configured but [location] is unset, log a
    # loud error and force the schedule off so the PowerManager
    # stays in always-AWAKE mode instead of computing sunrise for
    # (0, 0).
    if not _validate_schedule_config(config, log) and config.schedule.enabled:
        config.schedule.enabled = False

    state_dir = _resolve_state_dir(log)

    cameras = build_manager(config)
    power_manager = PowerManager(config, cameras, state_dir=state_dir)
    # Persistent camera failures (wedged SerDes link) escalate to the
    # PowerManager, which may self power-cycle the Pi as a last resort.
    cameras.attach_failure_callback(power_manager.handle_camera_failure)
    modem_probe = ModemProbe(config.network)
    server = StreamerServer(
        config, cameras, power=power_manager, modem=modem_probe
    )
    app = server.build()

    log.info(
        "Power: schedule_enabled=%s dry_run=%s keep_cameras_warm=%s",
        config.schedule.enabled,
        config.power.dry_run,
        config.power.keep_cameras_warm,
    )
    if config.power.recovery_power_cycle:
        log.info(
            "Recovery power-cycle: threshold=%d max/day=%d "
            "boot_grace=%dmin wake_delay=%ds",
            config.power.recovery_failure_threshold,
            config.power.recovery_max_cycles_per_day,
            config.power.recovery_boot_grace_minutes,
            config.power.recovery_wake_delay_seconds,
        )
    if config.schedule.enabled:
        log.info(
            "Schedule: lat=%.5f lon=%.5f tz=%s "
            "sunrise_offset=%+dm sunset_offset=%+dm "
            "warn_before=%dm wake_lead=%dm",
            config.location.latitude,
            config.location.longitude,
            config.location.timezone,
            config.schedule.sunrise_offset_minutes,
            config.schedule.sunset_offset_minutes,
            config.schedule.warn_minutes_before_sleep,
            config.schedule.wake_lead_minutes,
        )
    log.info(
        "Modem probe: target=%s interval=%ds timeout=%ds",
        config.network.modem_probe_target,
        config.network.modem_probe_interval_seconds,
        config.network.modem_probe_timeout_seconds,
    )

    async def _warmup_pipelines(_app: web.Application) -> None:
        """Warm the cameras, then hand them to the PowerManager.

        Order matters: PowerManager.start() does an immediate state
        evaluation, and we want the cameras already warmed and held
        by the time that runs. If the initial decision is ASLEEP,
        the PowerManager will release them again right away — see
        the inline note in ``power.py`` about boot-into-ASLEEP.
        """

        log = logging.getLogger("streamer.warmup")
        mode = "hold" if config.power.keep_cameras_warm else "touch"
        log.info("Warming up camera pipelines (concurrent, mode=%s)", mode)
        held = await _warmup_cameras(cameras, config, log)
        if config.power.keep_cameras_warm:
            log.info(
                "Warmup complete; %d camera(s) held warm; "
                "handing refcounts to power manager",
                len(held),
            )
        else:
            log.info(
                "Warmup complete; cameras released "
                "(idle stop in %ds)",
                config.power.idle_grace_seconds,
            )
        await power_manager.apply_initial_hold(held)
        await power_manager.start()
        await modem_probe.start()

    async def _on_cleanup(_app: web.Application) -> None:
        # Best-effort stop the periodic tasks before tearing down
        # cameras so they don't try to re-acquire a closed pipeline
        # mid-shutdown.
        await power_manager.stop()
        await modem_probe.stop()
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

"""Entrypoint: ``python -m streamer`` or the installed ``streamer`` script."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from aiohttp import web

from streamer import __version__
from streamer.cameras import build_manager
from streamer.config import load_config
from streamer.server import StreamerServer


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

    async def _on_cleanup(_app: web.Application) -> None:
        await cameras.shutdown()

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

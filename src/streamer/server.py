"""aiohttp server: static UI, JSON API, MJPEG streaming.

Single auth surface: every API and stream call requires the configured
``auth_token`` as either an ``Authorization: Bearer <token>`` header or
a ``?key=<token>`` query parameter. The viewer HTML, the landing page,
the static assets, and ``/health`` are unauthenticated so the browser
can load them anonymously and then carry the token on the data
endpoints.

Endpoints:

  Unauthenticated:
    GET /                        -> landing page linking /cam0 and /cam1
    GET /cam0, /cam1             -> per-camera viewer HTML
    GET /static/<file>           -> CSS / JS / images / favicon
    GET /health                  -> liveness probe

  Token-gated (Bearer header OR ?key=):
    GET  /api/status             -> JSON service + camera + power status
    GET  /api/info               -> site_name + per-camera display names
    GET  /api/admin/sleep-enabled  -> {"enabled": bool}
    POST /api/admin/sleep-enabled  -> body {"enabled": bool}, persists
                                       to /var/lib/streamer/sleep_enabled.json
    GET  /stream/cam0, /stream/cam1
                                 -> long-lived multipart/x-mixed-replace
                                    MJPEG; one capture loop per request

Phase 2 wires power-mode awareness into ``/stream/*``:

* In ``ENTERING_SLEEP``, responses carry a ``Warning: 299 -
  "sleeping in <N> minutes"`` header so the viewer UI can surface a
  countdown without polling another endpoint.
* In ``ASLEEP`` (reachable only under ``power.dry_run = true``; the
  real path halts the Pi), ``/stream/*`` returns ``503 SLEEPING`` so
  reconnecting viewers see a clean failure mode.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from streamer import __version__
from streamer.cameras import Camera, CameraManager
from streamer.config import AppConfig
from streamer.mjpeg import CONTENT_TYPE as MJPEG_CONTENT_TYPE
from streamer.mjpeg import encode_jpeg, part
from streamer.power import Mode

if TYPE_CHECKING:
    from streamer.modem import ModemProbe
    from streamer.power import PowerManager

logger = logging.getLogger("streamer.server")


WEBUI_DIR = Path(__file__).resolve().parent / "webui"

# Per-frame capture timeout. ``Camera.capture`` runs in a per-camera
# thread, so a TimeoutError here only unblocks the awaiting coroutine
# — the underlying thread is still wedged inside libcamera. We mark
# the camera broken so the next acquire rebuilds the picamera2
# instance.
#
# We split the timeout into two regimes because they have very
# different expected latencies:
#
#   * **First frame after a fresh acquire.** Has to wait for one full
#     ``FrameDurationLimits`` interval *and* for libcamera's IPA/3A
#     (AGC/AWB) to converge across several frames. At 1 fps this can
#     reach 5-10 s; ``FIRST_FRAME_TIMEOUT_SECONDS`` is set well above
#     that so a slow cold start isn't mistaken for a wedge.
#   * **Steady-state frames.** A healthy capture completes in ~50 ms
#     regardless of framerate. The effective steady-state timeout is
#     ``max(CAPTURE_TIMEOUT_FLOOR_SECONDS,
#     CAPTURE_TIMEOUT_INTERVAL_MULTIPLIER × frame_interval)``: tight
#     enough at 15 fps to detect a wedge quickly, generous enough at
#     1 fps to absorb scheduler jitter without false positives.
#
# After a recovery (mark_broken → release → re-acquire), the next
# capture is again considered "first frame" — the picamera2 instance
# was just rebuilt.
FIRST_FRAME_TIMEOUT_SECONDS = 15.0
CAPTURE_TIMEOUT_FLOOR_SECONDS = 2.0
CAPTURE_TIMEOUT_INTERVAL_MULTIPLIER = 3.0

# Paths that bypass auth entirely. The static UI bundle and the four
# HTML entrypoints (landing, cam pages, health) must be reachable
# anonymously so the browser can load them before it has a token.
# Everything data-bearing falls through to the token check below.
UNAUTH_EXACT = {"/", "/health", "/cam0", "/cam1"}
UNAUTH_PREFIXES = ("/static/",)


def _extract_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        if token:
            return token
    key = request.query.get("key", "").strip()
    return key or None


def _make_auth_middleware(auth_token: str):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if path in UNAUTH_EXACT or any(
            path.startswith(p) for p in UNAUTH_PREFIXES
        ):
            return await handler(request)
        provided = _extract_token(request)
        if provided is None:
            return web.json_response({"error": "missing access key"}, status=401)
        if provided != auth_token:
            return web.json_response({"error": "invalid access key"}, status=401)
        return await handler(request)

    return auth_middleware


class StreamerServer:
    def __init__(
        self,
        config: AppConfig,
        cameras: CameraManager,
        power: "PowerManager | None" = None,
        modem: "ModemProbe | None" = None,
    ) -> None:
        self.config = config
        self.cameras = cameras
        self.power = power
        self.modem = modem
        # Set of in-flight stream connections, for clean shutdown
        # and for the power state machine to cancel on ASLEEP entry.
        self._active_streams: set[asyncio.Task] = set()

    def build(self) -> web.Application:
        app = web.Application(
            middlewares=[_make_auth_middleware(self.config.server.auth_token)]
        )

        # Unauthenticated HTML / health.
        app.router.add_get("/", self._index)
        app.router.add_get("/cam0", self._camera_page)
        app.router.add_get("/cam1", self._camera_page)
        app.router.add_get("/health", self._health)

        # Token-gated JSON + MJPEG.
        app.router.add_get("/api/status", self._status)
        app.router.add_get("/api/info", self._info)
        app.router.add_get("/api/admin/sleep-enabled", self._get_sleep_enabled)
        app.router.add_post("/api/admin/sleep-enabled", self._set_sleep_enabled)
        app.router.add_get(r"/stream/cam{cam:\d+}", self._stream)

        # Static assets last so it doesn't shadow more specific routes.
        app.router.add_static("/static", WEBUI_DIR)

        # Let the power manager close all active streams on ASLEEP.
        if self.power is not None:
            self.power.attach_stream_canceller(self._cancel_all_streams)

        return app

    async def _cancel_all_streams(self) -> None:
        """Cancel every in-flight /stream/* task. Used by PowerManager
        on the ASLEEP transition (both real and dry_run).
        """

        if not self._active_streams:
            return
        n = len(self._active_streams)
        for task in list(self._active_streams):
            task.cancel()
        logger.info("Cancelled %d active stream(s) for sleep entry", n)

    # ---------- HTML / health ----------

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "index.html")

    async def _camera_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "cam.html")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__})

    # ---------- JSON API ----------

    async def _status(self, request: web.Request) -> web.Response:
        power_snap = self.power.snapshot() if self.power is not None else {
            "mode": "AWAKE",
            "sleep_enabled": True,
            "dry_run": False,
            "schedule_enabled": False,
            "next_event": None,
        }
        modem_snap = self.modem.snapshot() if self.modem is not None else None
        return web.json_response(
            {
                "version": __version__,
                "mode": power_snap["mode"],
                "sleep_enabled": power_snap["sleep_enabled"],
                "dry_run": power_snap["dry_run"],
                "schedule_enabled": power_snap["schedule_enabled"],
                "next_event": power_snap["next_event"],
                "site_name": self.config.server.site_name or "Streamer",
                "stream": {
                    "framerate": self.config.stream.framerate,
                    "jpeg_quality": self.config.stream.jpeg_quality,
                },
                "cameras": [
                    {
                        "camera_num": cam.camera_num,
                        "display_name": self._camera_display_name(cam.camera_num),
                        "running": cam.running,
                        "refcount": cam.refcount,
                        "resolution": list(cam.resolution),
                        "framerate": cam.framerate,
                    }
                    for cam in self.cameras.all()
                ],
                "active_streams": len(self._active_streams),
                "modem": modem_snap,
            }
        )

    # ---------- sleep override admin ----------

    async def _get_sleep_enabled(self, request: web.Request) -> web.Response:
        if self.power is None:
            return web.json_response({"enabled": True})
        return web.json_response({"enabled": self.power.sleep_enabled})

    async def _set_sleep_enabled(self, request: web.Request) -> web.Response:
        if self.power is None:
            return web.json_response(
                {"error": "power manager not configured"}, status=503
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "request body must be JSON"}, status=400
            )
        if not isinstance(body, dict) or "enabled" not in body:
            return web.json_response(
                {"error": "expected JSON object with 'enabled' field"},
                status=400,
            )
        new_value = await self.power.set_sleep_enabled(bool(body["enabled"]))
        return web.json_response({"enabled": new_value})

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "site_name": self.config.server.site_name or "Streamer",
                "cameras": [
                    {
                        "camera_num": cam.camera_num,
                        "display_name": self._camera_display_name(cam.camera_num),
                    }
                    for cam in self.cameras.all()
                ],
            }
        )

    def _camera_display_name(self, camera_num: int) -> str:
        cfg = getattr(self.config, f"camera{camera_num}", None)
        if cfg is not None and getattr(cfg, "name", ""):
            return cfg.name
        return f"Camera {camera_num}"

    # ---------- MJPEG stream ----------

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        try:
            camera_num = int(request.match_info["cam"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid camera number"}, status=400)
        if camera_num not in self.cameras.numbers():
            return web.json_response(
                {"error": f"unknown camera: {camera_num}"}, status=404
            )

        # Refuse new stream connections while the power state machine
        # has the service in ASLEEP. Only reachable in dry_run since
        # real ASLEEP halts the Pi before this code can run.
        if self.power is not None and self.power.mode == Mode.ASLEEP:
            return web.json_response(
                {"error": "service is in scheduled sleep"},
                status=503,
                reason="SLEEPING",
            )

        cam = self.cameras.get(camera_num)
        peer = request.remote
        log = logger.getChild(f"stream.cam{camera_num}")
        log.info("Stream open from %s", peer)

        # Response headers. During ENTERING_SLEEP we tack on a
        # ``Warning: 299`` so the viewer JS can show a sleep-soon
        # banner without polling another endpoint.
        headers = {
            "Content-Type": MJPEG_CONTENT_TYPE,
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "close",
        }
        if self.power is not None and self.power.mode == Mode.ENTERING_SLEEP:
            mins = self.power.minutes_until_sleep()
            if mins is not None:
                headers["Warning"] = (
                    f'299 - "sleeping in {mins} minutes"'
                )

        resp = web.StreamResponse(status=200, headers=headers)
        await resp.prepare(request)

        task = asyncio.current_task()
        if task is not None:
            self._active_streams.add(task)

        target_interval = 1.0 / max(self.config.stream.framerate, 0.01)
        steady_timeout = max(
            CAPTURE_TIMEOUT_FLOOR_SECONDS,
            CAPTURE_TIMEOUT_INTERVAL_MULTIPLIER * target_interval,
        )
        jpeg_quality = self.config.stream.jpeg_quality
        loop = asyncio.get_running_loop()
        frames_written = 0

        # We manage acquire/release manually rather than using
        # ``async with cam.session()`` so that a capture timeout can
        # mark the camera broken, release the refcount, and re-acquire
        # to trigger the recovery path — all while keeping the same
        # HTTP connection (and the user's <img>) alive.
        acquired = False
        # Reset to 0 on every (re-)acquire so the next capture uses
        # the generous first-frame timeout.
        frames_since_acquire = 0
        try:
            while True:
                if not acquired:
                    try:
                        await cam.acquire()
                    except Exception:
                        log.exception("Camera acquire failed; closing stream")
                        break
                    acquired = True
                    frames_since_acquire = 0

                cycle_start = time.monotonic()
                this_timeout = (
                    FIRST_FRAME_TIMEOUT_SECONDS
                    if frames_since_acquire == 0
                    else steady_timeout
                )
                try:
                    array = await asyncio.wait_for(
                        cam.capture(), timeout=this_timeout
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    log.warning(
                        "Capture timeout on camera %d after %.1fs "
                        "(frames_since_acquire=%d); recovering",
                        camera_num,
                        this_timeout,
                        frames_since_acquire,
                    )
                    cam.mark_broken()
                    await cam.release()
                    acquired = False
                    # Brief pause before the next acquire so we don't
                    # spin tightly through repeated recovery attempts
                    # if libcamera is genuinely down.
                    await asyncio.sleep(target_interval)
                    continue

                frames_since_acquire += 1
                jpeg = await loop.run_in_executor(
                    None, encode_jpeg, array, jpeg_quality
                )
                try:
                    await resp.write(part(jpeg))
                except (ConnectionResetError, asyncio.CancelledError):
                    raise
                except Exception:
                    log.exception("Write failed; closing stream")
                    break
                frames_written += 1

                elapsed = time.monotonic() - cycle_start
                remaining = target_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except (ConnectionResetError, asyncio.CancelledError):
            # Normal: client disconnected, or server shutdown.
            pass
        except Exception:
            log.exception("Stream loop crashed")
        finally:
            if acquired:
                try:
                    await cam.release()
                except Exception:
                    log.exception("Release failed")
            if task is not None:
                self._active_streams.discard(task)
            log.info(
                "Stream closed for %s after %d frames", peer, frames_written
            )
            try:
                await resp.write_eof()
            except Exception:
                pass

        return resp

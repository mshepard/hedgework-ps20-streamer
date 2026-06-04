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
    GET /api/status              -> JSON service + camera status
    GET /api/info                -> site_name + per-camera display names
    GET /stream/cam0, /stream/cam1
                                 -> long-lived multipart/x-mixed-replace
                                    MJPEG; one capture loop per request

Phase 2 will fold a real power-mode state machine into ``/api/status``
and gate the ``/stream/*`` endpoints on mode (returning 503 SLEEPING
during scheduled halts, primarily relevant in ``dry_run`` since a real
HARD_SLEEP halts the Pi).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from aiohttp import web

from streamer import __version__
from streamer.cameras import Camera, CameraManager
from streamer.config import AppConfig
from streamer.mjpeg import CONTENT_TYPE as MJPEG_CONTENT_TYPE
from streamer.mjpeg import encode_jpeg, part

logger = logging.getLogger("streamer.server")


WEBUI_DIR = Path(__file__).resolve().parent / "webui"

# Per-frame capture timeout. ``Camera.capture`` runs in a per-camera
# thread, so a TimeoutError here only unblocks the awaiting coroutine —
# the underlying thread is still wedged inside libcamera. We mark the
# camera broken so the next acquire rebuilds the picamera2 instance.
# Healthy captures complete in ~50 ms; 2 s is a comfortable ceiling.
CAPTURE_TIMEOUT_SECONDS = 2.0

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
    def __init__(self, config: AppConfig, cameras: CameraManager) -> None:
        self.config = config
        self.cameras = cameras
        # Set of in-flight stream connections, for clean shutdown.
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
        app.router.add_get(r"/stream/cam{cam:\d+}", self._stream)

        # Static assets last so it doesn't shadow more specific routes.
        app.router.add_static("/static", WEBUI_DIR)

        return app

    # ---------- HTML / health ----------

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "index.html")

    async def _camera_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEBUI_DIR / "cam.html")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__})

    # ---------- JSON API ----------

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "version": __version__,
                # Phase 2 replaces this stub with the real state machine.
                "mode": "AWAKE",
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
            }
        )

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

        cam = self.cameras.get(camera_num)
        peer = request.remote
        log = logger.getChild(f"stream.cam{camera_num}")
        log.info("Stream open from %s", peer)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": MJPEG_CONTENT_TYPE,
                "Cache-Control": "no-store, no-cache, must-revalidate, private",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
                "Connection": "close",
            },
        )
        await resp.prepare(request)

        task = asyncio.current_task()
        if task is not None:
            self._active_streams.add(task)

        target_interval = 1.0 / max(self.config.stream.framerate, 0.01)
        jpeg_quality = self.config.stream.jpeg_quality
        loop = asyncio.get_running_loop()
        frames_written = 0

        # We manage acquire/release manually rather than using
        # ``async with cam.session()`` so that a capture timeout can
        # mark the camera broken, release the refcount, and re-acquire
        # to trigger the recovery path — all while keeping the same
        # HTTP connection (and the user's <img>) alive.
        acquired = False
        try:
            while True:
                if not acquired:
                    try:
                        await cam.acquire()
                    except Exception:
                        log.exception("Camera acquire failed; closing stream")
                        break
                    acquired = True

                cycle_start = time.monotonic()
                try:
                    array = await asyncio.wait_for(
                        cam.capture(), timeout=CAPTURE_TIMEOUT_SECONDS
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    log.warning(
                        "Capture timeout on camera %d; recovering",
                        camera_num,
                    )
                    cam.mark_broken()
                    await cam.release()
                    acquired = False
                    # Brief pause before the next acquire so we don't
                    # spin tightly through repeated recovery attempts
                    # if libcamera is genuinely down.
                    await asyncio.sleep(target_interval)
                    continue

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

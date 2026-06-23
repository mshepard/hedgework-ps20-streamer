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
    GET /embed                   -> copy-pasteable cross-origin embed snippet (legacy dual-tile)
    GET /embed/cam0, /embed/cam1 -> per-camera embed demo + docs
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

  Conditionally public (when ``[server] public_streams = true``):
    GET  /api/public/status      -> minimal CORS-friendly viewer state
                                    (mode, next_event, site_name,
                                    cameras, stream.framerate)
    GET  /stream/cam0, /stream/cam1
                                 -> same MJPEG endpoints, with CORS
                                    headers and no token check, so an
                                    `<img>` on a third-party page can
                                    load them

  Public-mode CORS surface: requests to ``/api/public/*`` and
  ``/stream/*`` get ``Access-Control-Allow-Origin: *`` plus an
  ``Access-Control-Expose-Headers: Warning`` so the embed JS can read
  the ``Warning: 299 - "sleeping in N minutes"`` countdown on the
  ENTERING_SLEEP MJPEG response. The CORS middleware is installed
  unconditionally so OPTIONS preflights always work; the auth check
  is what gates real access when ``public_streams = false``.

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
from streamer.mjpeg import part
from streamer.power import Mode

if TYPE_CHECKING:
    from streamer.modem import ModemProbe
    from streamer.power import PowerManager
    from streamer.wildlife.manager import WildlifeManager

logger = logging.getLogger("streamer.server")


WEBUI_DIR = Path(__file__).resolve().parent / "webui"

# Paths that bypass auth entirely. The static UI bundle, the four
# HTML entrypoints (landing, cam pages, embed snippet), and ``/health``
# must be reachable anonymously so the browser can load them before
# it has a token. Everything data-bearing falls through to the token
# check below.
UNAUTH_EXACT = {
    "/",
    "/health",
    "/cam0",
    "/cam1",
    "/embed",
    "/embed/cam0",
    "/embed/cam1",
}
UNAUTH_PREFIXES = ("/static/",)

# Paths that may be served anonymously when ``[server] public_streams``
# is true. The auth middleware skips the token check for these paths
# in that case; otherwise they fall through to the normal token
# requirement. CORS headers are added on the response for paths
# matching these prefixes regardless of public_streams, so a 401
# from public mode = false still surfaces correctly cross-origin.
PUBLIC_PREFIXES = ("/api/public/", "/stream/")

# CORS preflight cache lifetime. 24h means a long-lived embedded page
# won't pay the OPTIONS round-trip more than once a day.
CORS_PREFLIGHT_MAX_AGE = "86400"


def _extract_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        if token:
            return token
    key = request.query.get("key", "").strip()
    return key or None


def _is_public_path(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _make_auth_middleware(auth_token: str, public_streams: bool):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if path in UNAUTH_EXACT or any(
            path.startswith(p) for p in UNAUTH_PREFIXES
        ):
            return await handler(request)
        # CORS preflight is always anonymous: the actual auth check
        # happens on the follow-up real request. Without this, an
        # OPTIONS to /stream/cam0 from a public WordPress page would
        # 401 and the browser would refuse to send the real GET.
        if request.method == "OPTIONS":
            return await handler(request)
        # ``public_streams`` opens /api/public/* and /stream/* to
        # anonymous viewers so they can be embedded cross-origin.
        # All other paths (admin, /api/status, /api/info) keep their
        # bearer-token requirement.
        if public_streams and _is_public_path(path):
            return await handler(request)
        provided = _extract_token(request)
        if provided is None:
            return web.json_response({"error": "missing access key"}, status=401)
        if provided != auth_token:
            return web.json_response({"error": "invalid access key"}, status=401)
        return await handler(request)

    return auth_middleware


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    """Add CORS headers to public endpoints and answer preflights.

    Installed unconditionally so OPTIONS preflights and cross-origin
    responses work whenever the underlying endpoint allows them. When
    ``public_streams = false``, /api/public/* and /stream/* still
    return 401 on real requests — but with CORS headers attached so
    the embed page's fetch error message accurately reflects "401" and
    not the generic "CORS error" the browser would otherwise show.
    """

    path = request.path
    # OPTIONS preflight: short-circuit with the headers the browser
    # needs. Browsers send a preflight before any cross-origin
    # request that uses non-simple methods/headers; for our minimal
    # GET-with-?key= traffic we don't strictly need to permit
    # Authorization, but allowing it lets a future caller use the
    # Bearer header cross-origin too.
    if request.method == "OPTIONS" and _is_public_path(path):
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Max-Age": CORS_PREFLIGHT_MAX_AGE,
            },
        )

    response = await handler(request)

    if _is_public_path(path):
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        # Expose the Warning header so the embed JS can read the
        # "sleeping in N minutes" countdown the stream handler attaches
        # during ENTERING_SLEEP. Default browser CORS hides everything
        # except a short safelist.
        response.headers.setdefault(
            "Access-Control-Expose-Headers", "Warning"
        )

    return response


class StreamerServer:
    def __init__(
        self,
        config: AppConfig,
        cameras: CameraManager,
        power: "PowerManager | None" = None,
        modem: "ModemProbe | None" = None,
        wildlife: "WildlifeManager | None" = None,
    ) -> None:
        self.config = config
        self.cameras = cameras
        self.power = power
        self.modem = modem
        self.wildlife = wildlife
        # Set of in-flight stream connections, for clean shutdown
        # and for the power state machine to cancel on ASLEEP entry.
        self._active_streams: set[asyncio.Task] = set()

    def build(self) -> web.Application:
        # CORS first so OPTIONS preflights short-circuit before the
        # auth check; auth second so token validation runs on every
        # data-bearing request that survives the preflight.
        app = web.Application(
            middlewares=[
                _cors_middleware,
                _make_auth_middleware(
                    self.config.server.auth_token,
                    self.config.server.public_streams,
                ),
            ]
        )

        # Unauthenticated HTML / health.
        app.router.add_get("/", self._index)
        app.router.add_get("/cam0", self._camera_page)
        app.router.add_get("/cam1", self._camera_page)
        app.router.add_get("/embed", self._embed_page)
        app.router.add_get("/embed/cam0", self._embed_cam_page)
        app.router.add_get("/embed/cam1", self._embed_cam_page)
        app.router.add_get("/health", self._health)

        # Public (anonymous when public_streams=true; token-gated otherwise).
        app.router.add_get("/api/public/status", self._public_status)

        # Token-gated JSON + MJPEG.
        app.router.add_get("/api/status", self._status)
        app.router.add_get("/api/info", self._info)
        app.router.add_get("/api/admin/sleep-enabled", self._get_sleep_enabled)
        app.router.add_post("/api/admin/sleep-enabled", self._set_sleep_enabled)
        app.router.add_get("/api/wildlife/recent", self._wildlife_recent)
        app.router.add_get("/api/wildlife/counts", self._wildlife_counts)
        app.router.add_get(
            r"/api/wildlife/images/{detection_id:\d+}", self._wildlife_image
        )
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

    async def _embed_page(self, request: web.Request) -> web.Response:
        # Legacy dual-tile embed snippet.
        return web.FileResponse(WEBUI_DIR / "embed.html")

    async def _embed_cam_page(self, request: web.Request) -> web.Response:
        # Per-camera embed demo + documentation. Camera number is
        # inferred from the URL path (/embed/cam0, /embed/cam1).
        return web.FileResponse(WEBUI_DIR / "embed-cam.html")

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

    # ---------- Public (CORS-enabled, anonymous when public_streams=true) ----------

    async def _public_status(self, request: web.Request) -> web.Response:
        """Minimal status view safe to expose anonymously.

        Includes only what an external embed needs to render its UI:
        the schedule mode, the next sleep/wake event for the countdown
        banner, the site brand, and the list of cameras + framerate.
        Deliberately excludes anything operational (refcounts, modem
        latency, dry_run, schedule_enabled internals beyond mode) so
        a public embed page can't fingerprint the deployment beyond
        what it would already see from the live MJPEG feed.
        """

        power_snap = self.power.snapshot() if self.power is not None else {
            "mode": "AWAKE",
            "next_event": None,
        }
        schedule_window = (
            self.power.schedule_window() if self.power is not None else None
        )
        return web.json_response(
            {
                "mode": power_snap["mode"],
                "next_event": power_snap["next_event"],
                # Both bounds of the upcoming off-window, for client-side
                # caching: the embed uses it to show "asleep until HH:MM"
                # while the Pi is powered off and unreachable.
                "schedule": schedule_window,
                "site_name": self.config.server.site_name or "Streamer",
                "stream": {
                    "framerate": self.config.stream.framerate,
                    "max_duration_seconds": self.config.stream.max_duration_seconds,
                },
                "cameras": [
                    {
                        "camera_num": cam.camera_num,
                        "display_name": self._camera_display_name(cam.camera_num),
                        "resolution": list(cam.resolution),
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

    # ---------- Wildlife API ----------

    async def _wildlife_recent(self, request: web.Request) -> web.Response:
        if self.wildlife is None or not self.wildlife.enabled:
            return web.json_response({"detections": []})
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            return web.json_response({"error": "invalid limit"}, status=400)
        limit = max(1, min(limit, 100))
        conn = await self.wildlife.database.connect()
        try:
            rows = await self.wildlife.database.recent(conn, limit=limit)
        finally:
            await conn.close()
        from streamer.wildlife.db import WildlifeDatabase

        return web.json_response(
            {
                "detections": [
                    WildlifeDatabase.row_to_json(row) for row in rows
                ]
            }
        )

    async def _wildlife_counts(self, request: web.Request) -> web.Response:
        if self.wildlife is None or not self.wildlife.enabled:
            return web.json_response({"counts": []})
        period = request.query.get("period", "today")
        if period != "today":
            return web.json_response(
                {"error": "only period=today is supported"}, status=400
            )
        conn = await self.wildlife.database.connect()
        try:
            counts = await self.wildlife.database.counts_today(conn)
        finally:
            await conn.close()
        return web.json_response({"counts": counts})

    async def _wildlife_image(self, request: web.Request) -> web.Response:
        if self.wildlife is None or not self.wildlife.enabled:
            return web.json_response({"error": "wildlife disabled"}, status=404)
        try:
            detection_id = int(request.match_info["detection_id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid id"}, status=400)
        conn = await self.wildlife.database.connect()
        try:
            row = await self.wildlife.database.get_by_id(conn, detection_id)
        finally:
            await conn.close()
        if row is None:
            return web.json_response({"error": "not found"}, status=404)
        path = Path(row["image_path"])
        if not path.is_file():
            return web.json_response({"error": "image missing"}, status=404)
        return web.FileResponse(path)

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

        publisher = self.cameras.publisher(camera_num)
        peer = request.remote
        log = logger.getChild(f"stream.cam{camera_num}")
        log.info("Stream open from %s", peer)

        # Response headers. During ENTERING_SLEEP we tack on a
        # ``Warning: 299`` so the viewer JS can show a sleep-soon
        # banner without polling another endpoint.
        #
        # CORS headers are set here rather than relying on the global
        # ``_cors_middleware``: ``StreamResponse.prepare()`` flushes
        # headers to the wire immediately, before any post-handler
        # middleware can mutate them. Setting them in the dict that
        # ``StreamResponse(..., headers=)`` receives means they're
        # present in that first flush. Harmless when the request is
        # same-origin (browser ignores the matching origin).
        headers = {
            "Content-Type": MJPEG_CONTENT_TYPE,
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            "Connection": "close",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Warning",
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

        frames_written = 0
        last_generation = -1
        max_duration = self.config.stream.max_duration_seconds
        stream_start = time.monotonic()
        timed_out = False

        try:
            while True:
                if (
                    max_duration > 0
                    and time.monotonic() - stream_start >= max_duration
                ):
                    timed_out = True
                    log.info(
                        "Stream max duration (%ds) reached; closing",
                        max_duration,
                    )
                    break
                try:
                    frame = await publisher.wait_frame(last_generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Publisher wait failed; closing stream")
                    break

                last_generation = frame.generation
                try:
                    await resp.write(part(frame.jpeg))
                except (ConnectionResetError, asyncio.CancelledError):
                    raise
                except Exception:
                    log.exception("Write failed; closing stream")
                    break
                frames_written += 1
        except (ConnectionResetError, asyncio.CancelledError):
            # Normal: client disconnected, or server shutdown.
            pass
        except Exception:
            log.exception("Stream loop crashed")
        finally:
            if task is not None:
                self._active_streams.discard(task)
            log.info(
                "Stream closed for %s after %d frames%s",
                peer,
                frames_written,
                " (max duration)" if timed_out else "",
            )
            try:
                await resp.write_eof()
            except Exception:
                pass

        return resp

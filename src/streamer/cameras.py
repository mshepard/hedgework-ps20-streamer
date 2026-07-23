"""Camera lifecycle manager.

Wraps two ``Picamera2`` instances with reference-counted start/stop. Frames
are produced in worker threads (picamera2 calls are blocking) and exposed
to async consumers as RGB-ordered numpy arrays of shape ``(H, W, 3)``,
suitable for ``Pillow.Image.fromarray(..., mode="RGB")`` and JPEG encode.

Pixel-format note: ``picamera2`` has the historical quirk that its
``"RGB888"`` format produces BGR-ordered data in numpy, and its
``"BGR888"`` format produces RGB-ordered data. We use ``BGR888`` so
callers receive RGB without an explicit channel swap.

Reliability design:

* Each camera owns a dedicated single-thread ``ThreadPoolExecutor``. All
  blocking picamera2 calls (start / stop / capture / recover) run on it.
  When ``capture_array`` wedges inside libcamera (an empirically real
  failure mode on multi-camera Pi 5 setups), only that camera's thread
  is lost — every other coroutine, including the other camera, keeps
  running.
* A short ``acquire`` / ``capture`` / ``release`` cycle is cheap because
  the ``idle_grace_seconds`` defer-stop lets fast reconnects skip the
  ~150 ms picamera2 init.
* ``mark_broken()`` flags a wedged instance from the outside (e.g. a
  capture timeout in the publisher loop). It is a no-op while the
  camera is already broken — the next ``acquire()`` owns recovery.
  The first call per wedge swaps the per-camera executor so recovery
  has a free thread even though the previous one is stuck in libcamera.
* The sensor's ``FrameDurationLimits`` is driven by the stream framerate,
  so producer and consumer run at the same cadence. That removes the
  slow-consumer buffer-pool stall pattern entirely. ``buffer_count=2``
  is still small enough that even momentary cadence drift doesn't let
  stale buffers accumulate.

Phase 2 will add a ``power.py``-controlled quality clamp here so the
schedule layer can downshift framerate or resolution without restarting
the service.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import numpy as np

if TYPE_CHECKING:
    from picamera2 import Picamera2  # imported lazily at runtime

from streamer.config import AppConfig, CameraConfig, StreamConfig
from streamer.mjpeg import encode_jpeg

logger = logging.getLogger("streamer.cameras")

# Capture timeouts for the shared per-camera publisher loop (same
# rationale as server.py's stream handler).
FIRST_FRAME_TIMEOUT_SECONDS = 15.0
CAPTURE_TIMEOUT_FLOOR_SECONDS = 2.0
CAPTURE_TIMEOUT_INTERVAL_MULTIPLIER = 3.0

# Minimum gap between ``acquire()`` recovery attempts while a camera
# stays ``_broken``. Grows with ``consecutive_failures`` so a wedged
# link does not spin at 1 Hz opening Picamera2 instances (and leaking
# FDs) for hours.
RECOVERY_BACKOFF_BASE_SECONDS = 5.0
RECOVERY_BACKOFF_CAP_SECONDS = 60.0


class RecoveryBackoff(Exception):
    """``acquire()`` refused a back-to-back recovery attempt."""

    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        super().__init__(delay_seconds)


class PublishedFrame:
    """One captured frame plus a pre-encoded JPEG for MJPEG subscribers."""

    __slots__ = ("generation", "rgb", "jpeg", "captured_at")

    def __init__(
        self,
        generation: int,
        rgb: np.ndarray,
        jpeg: bytes,
        captured_at: float,
    ) -> None:
        self.generation = generation
        self.rgb = rgb
        self.jpeg = jpeg
        self.captured_at = captured_at


class FramePublisher:
    """Single capture-and-encode loop per camera; fans out to subscribers.

    MJPEG stream handlers and the wildlife detector both await
    ``wait_frame()`` instead of calling ``Camera.capture()`` directly,
    so N concurrent viewers receive the full configured framerate
    without N-fold camera work.
    """

    def __init__(
        self,
        camera: Camera,
        framerate: float,
        jpeg_quality: int,
    ) -> None:
        self._camera = camera
        self._framerate = framerate
        self._jpeg_quality = jpeg_quality
        self._latest: PublishedFrame | None = None
        self._frame_ready = asyncio.Event()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def camera_num(self) -> int:
        return self._camera.camera_num

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
            self._task = asyncio.create_task(
                self._capture_loop(),
                name=f"cam{self._camera.camera_num}-publisher",
            )

    async def stop(self) -> None:
        async with self._lock:
            if not self._running:
                return
            self._running = False
            if self._task is not None:
                self._task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._task
                self._task = None

    async def wait_frame(self, after_generation: int = -1) -> PublishedFrame:
        """Block until a frame newer than ``after_generation`` is ready."""

        while True:
            latest = self._latest
            if latest is not None and latest.generation > after_generation:
                return latest
            self._frame_ready.clear()
            await self._frame_ready.wait()

    async def _capture_loop(self) -> None:
        cam = self._camera
        log = logger.getChild(f"publisher.cam{cam.camera_num}")
        target_interval = 1.0 / max(self._framerate, 0.01)
        steady_timeout = max(
            CAPTURE_TIMEOUT_FLOOR_SECONDS,
            CAPTURE_TIMEOUT_INTERVAL_MULTIPLIER * target_interval,
        )
        loop = asyncio.get_running_loop()
        generation = 0
        acquired = False
        frames_since_acquire = 0

        try:
            while self._running:
                if not acquired:
                    try:
                        await cam.acquire()
                    except RecoveryBackoff as exc:
                        await asyncio.sleep(exc.delay_seconds)
                        continue
                    except Exception:
                        log.exception("Publisher acquire failed")
                        await asyncio.sleep(
                            cam._publisher_backoff_seconds(target_interval)
                        )
                        continue
                    acquired = True
                    frames_since_acquire = 0

                cycle_start = time.monotonic()
                this_timeout = (
                    FIRST_FRAME_TIMEOUT_SECONDS
                    if frames_since_acquire == 0
                    else steady_timeout
                )
                try:
                    rgb = await asyncio.wait_for(
                        cam.capture(), timeout=this_timeout
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    log.warning(
                        "Publisher capture timeout on camera %d; recovering",
                        cam.camera_num,
                    )
                    cam.mark_broken()
                    await cam.release()
                    acquired = False
                    await asyncio.sleep(
                        cam._publisher_backoff_seconds(target_interval)
                    )
                    continue

                frames_since_acquire += 1
                jpeg = await loop.run_in_executor(
                    None, encode_jpeg, rgb, self._jpeg_quality
                )
                generation += 1
                self._latest = PublishedFrame(
                    generation, rgb, jpeg, time.time()
                )
                self._frame_ready.set()

                elapsed = time.monotonic() - cycle_start
                remaining = target_interval - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass
        finally:
            if acquired:
                try:
                    await cam.release()
                except Exception:
                    log.exception(
                        "Publisher release failed for camera %d",
                        cam.camera_num,
                    )
            log.info("Publisher stopped for camera %d", cam.camera_num)


class Camera:
    """Single picamera2 device with refcounted start/stop."""

    def __init__(
        self,
        camera_num: int,
        resolution: tuple[int, int],
        framerate: float,
        controls: dict[str, Any] | None = None,
        idle_grace_seconds: int = 10,
        inference_resolution: tuple[int, int] | None = None,
    ) -> None:
        self.camera_num = camera_num
        self.resolution = resolution
        self.framerate = framerate
        self.controls = dict(controls or {})
        self.idle_grace_seconds = idle_grace_seconds
        # When set, configure a high-res ``main`` stream for wildlife
        # stills and a ``lores`` stream at ``resolution`` for MJPEG.
        self.inference_resolution = inference_resolution

        self._picam2: Picamera2 | None = None
        self._refcount: int = 0
        self._refcount_lock = asyncio.Lock()
        # Serialises ``capture_array`` calls against ``stop()`` /
        # ``close()``. Rebound on recovery so a leaked thread still
        # holding the old lock can't deadlock the new pipeline.
        self._device_lock = threading.Lock()
        self._stop_task: asyncio.Task[None] | None = None
        # Set by ``mark_broken()`` and consumed by the next
        # ``acquire()``, which tears the abandoned instance down and
        # opens a fresh one.
        self._broken: bool = False
        # Dedicated single-thread executor for blocking picamera2 calls.
        # Isolates wedged libcamera operations from the default thread
        # pool (HTTP body writes, JPEG encode, the other camera).
        # Replaced by the first ``mark_broken()`` in a wedge episode so
        # recovery has a free thread even though the previous one is
        # stuck. Further ``mark_broken()`` calls are a no-op.
        self._picam2_executor: ThreadPoolExecutor = self._make_executor()
        # Monotonic timestamp of the last ``_recover_blocking`` attempt.
        self._last_recovery_attempt: float = 0.0
        # Consecutive ``mark_broken()`` calls without an intervening
        # successful capture. Feeds the PowerManager's self power-cycle
        # trigger: a count that keeps climbing means the in-process
        # recovery path (rebuild picamera2 instance) isn't enough and
        # the camera link itself is wedged. Reset by the worker thread
        # on every good frame — plain int assignment, GIL-atomic.
        self.consecutive_failures: int = 0
        # Fired (as a task on the running loop) from ``mark_broken()``
        # with (camera_num, consecutive_failures). Set via
        # ``CameraManager.attach_failure_callback``.
        self._failure_cb: Callable[[int, int], Awaitable[None]] | None = None

    @property
    def dual_stream(self) -> bool:
        return self.inference_resolution is not None

    @property
    def stream_capture_name(self) -> str:
        """Stream name used for MJPEG / publisher frames."""

        return "lores" if self.dual_stream else "main"

    @property
    def running(self) -> bool:
        return self._picam2 is not None

    @property
    def refcount(self) -> int:
        return self._refcount

    def _make_executor(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"cam{self.camera_num}-picam2",
        )

    def mark_broken(self) -> None:
        """Signal that this camera's capture pipeline is wedged.

        The next ``acquire()`` rebuilds the picamera2 instance and
        rebinds ``_device_lock``. The executor is swapped on the first
        call in a wedge episode so recovery has a thread to run on —
        the previous worker is presumed stuck inside libcamera.

        Idempotent: while ``_broken`` is already set, this is a no-op.
        Recovery backoff and the next ``acquire()`` own further work.
        """

        if self._broken:
            return

        self._broken = True
        self.consecutive_failures += 1
        old_exec = self._picam2_executor
        self._picam2_executor = self._make_executor()
        old_exec.shutdown(wait=False, cancel_futures=True)

        if self._failure_cb is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No loop on this thread (shouldn't happen in practice;
                # mark_broken is called from async handlers). Skip the
                # notification rather than crash the recovery path.
                pass
            else:
                loop.create_task(
                    self._failure_cb(
                        self.camera_num, self.consecutive_failures
                    ),
                    name=f"cam{self.camera_num}-failure-cb",
                )

    def _recovery_backoff_seconds(self) -> float:
        """Backoff before retrying ``_recover_blocking`` after failures."""

        if self.consecutive_failures <= 1:
            return 0.0
        exponent = min(self.consecutive_failures - 2, 3)
        return min(
            RECOVERY_BACKOFF_CAP_SECONDS,
            RECOVERY_BACKOFF_BASE_SECONDS * (2**exponent),
        )

    def _publisher_backoff_seconds(self, target_interval: float) -> float:
        """Publisher sleep after a failed capture/recover cycle."""

        if self.consecutive_failures < 3:
            return target_interval
        exponent = min(self.consecutive_failures - 2, 5)
        return min(
            RECOVERY_BACKOFF_CAP_SECONDS,
            target_interval * (2**exponent),
        )

    async def acquire(self) -> None:
        async with self._refcount_lock:
            self._refcount += 1
            if self._stop_task is not None and not self._stop_task.done():
                self._stop_task.cancel()
                self._stop_task = None
            try:
                if self._broken:
                    backoff = self._recovery_backoff_seconds()
                    if backoff > 0.0:
                        elapsed = time.monotonic() - self._last_recovery_attempt
                        remaining = backoff - elapsed
                        if remaining > 0.0:
                            self._refcount -= 1
                            raise RecoveryBackoff(remaining)

                    logger.warning(
                        "Camera %d marked broken; abandoning instance and reopening",
                        self.camera_num,
                    )
                    self._last_recovery_attempt = time.monotonic()
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self._picam2_executor, self._recover_blocking
                    )
                    self._broken = False
                elif self._picam2 is None:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        self._picam2_executor, self._start_blocking
                    )
            except Exception:
                # Recover failed with ``_broken`` still set — count it so
                # backoff escalates and the power manager can intervene.
                if self._broken:
                    self.consecutive_failures += 1
                    if self._failure_cb is not None:
                        loop = asyncio.get_running_loop()
                        loop.create_task(
                            self._failure_cb(
                                self.camera_num, self.consecutive_failures
                            ),
                            name=f"cam{self.camera_num}-failure-cb",
                        )
                # Don't leak a refcount if start/recover failed; the
                # next acquire will retry from a clean slate.
                self._refcount -= 1
                raise

    async def release(self) -> None:
        async with self._refcount_lock:
            if self._refcount > 0:
                self._refcount -= 1
            if self._refcount == 0 and self._picam2 is not None:
                self._stop_task = asyncio.create_task(self._delayed_stop())

    async def _delayed_stop(self) -> None:
        try:
            await asyncio.sleep(self.idle_grace_seconds)
        except asyncio.CancelledError:
            return
        async with self._refcount_lock:
            if self._refcount == 0 and self._picam2 is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    self._picam2_executor, self._stop_blocking
                )

    def _start_blocking(self) -> None:
        from picamera2 import Picamera2  # noqa: PLC0415  (deferred import)

        width, height = self.resolution
        frame_duration_us = int(round(1_000_000 / self.framerate))
        # ``BGR888`` in picamera2 returns numpy in RGB channel order. Pick
        # this so Pillow's mode="RGB" consumes the array directly with no
        # explicit swap. ``buffer_count=2`` is the minimum that lets the
        # capture loop run smoothly without letting stale buffers
        # accumulate — higher values stalled under the dual-IMX708 setup.
        controls = {
            "FrameDurationLimits": (frame_duration_us, frame_duration_us),
            **self.controls,
        }
        picam2 = Picamera2(camera_num=self.camera_num)
        if self.inference_resolution is not None:
            inf_w, inf_h = self.inference_resolution
            logger.info(
                "Starting camera %d dual-stream: main=%dx%d lores=%dx%d @ %.2f fps",
                self.camera_num,
                inf_w,
                inf_h,
                width,
                height,
                self.framerate,
            )
            config = picam2.create_video_configuration(
                main={"size": (inf_w, inf_h), "format": "BGR888"},
                lores={"size": (width, height), "format": "BGR888"},
                controls=controls,
                buffer_count=2,
            )
        else:
            logger.info(
                "Starting camera %d at %dx%d @ %.2f fps",
                self.camera_num,
                width,
                height,
                self.framerate,
            )
            config = picam2.create_video_configuration(
                main={"size": (width, height), "format": "BGR888"},
                controls=controls,
                buffer_count=2,
            )
        picam2.configure(config)
        picam2.start()
        self._picam2 = picam2

    def _stop_blocking(self) -> None:
        picam2 = self._picam2
        if picam2 is None:
            return
        logger.info("Stopping camera %d", self.camera_num)
        try:
            picam2.stop()
            picam2.close()
        finally:
            self._picam2 = None

    def _recover_blocking(self) -> None:
        """Drop a wedged ``Picamera2`` instance and start a fresh one.

        The leaked executor thread stuck in ``capture_array`` may still
        hold the old ``_device_lock``, so we rebind that here too. We
        attempt ``close()`` on the old instance from a daemon thread
        with a short ceiling — ``close()`` itself can block forever on
        a wedged libcamera pipeline, but we don't want to wait. If the
        daemon doesn't finish in time we proceed without it; libcamera
        may then refuse the reopen, in which case ``_start_blocking``
        raises and the caller's ``acquire()`` rolls back the refcount.
        """

        old_picam2 = self._picam2
        self._picam2 = None
        self._device_lock = threading.Lock()

        if old_picam2 is not None:
            done = threading.Event()

            def _closer() -> None:
                try:
                    old_picam2.close()
                except Exception:
                    logger.exception(
                        "Force-close of camera %d failed", self.camera_num
                    )
                finally:
                    done.set()

            threading.Thread(
                target=_closer,
                daemon=True,
                name=f"cam{self.camera_num}-recover-close",
            ).start()
            if not done.wait(timeout=3.0):
                logger.error(
                    "Camera %d close() hung during recovery; "
                    "abandoning old picamera2 instance",
                    self.camera_num,
                )

        self._start_blocking()

    async def capture(self) -> np.ndarray:
        """Capture one MJPEG/publisher frame as an RGB (H, W, 3) array.

        Uses the lores stream when dual-stream (tiled inference) is
        enabled; otherwise the single main stream.
        """

        return await self._capture_stream(self.stream_capture_name)

    async def capture_main(self) -> np.ndarray:
        """Capture the high-res main stream (for tiled wildlife inference)."""

        return await self._capture_stream("main")

    async def _capture_stream(self, stream_name: str) -> np.ndarray:
        if self._picam2 is None:
            raise RuntimeError(f"Camera {self.camera_num} is not running")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._picam2_executor,
            self._capture_blocking,
            stream_name,
        )

    def _capture_blocking(self, stream_name: str = "main") -> np.ndarray:
        with self._device_lock:
            picam2 = self._picam2
            if picam2 is None:
                raise RuntimeError(f"Camera {self.camera_num} is not running")
            # Defensive copy: picamera2.capture_array() may, on some
            # libcamera versions / multi-camera setups, hand back a view
            # into a shared buffer pool. Copying decouples the two
            # cameras' frame streams.
            frame = picam2.capture_array(stream_name).copy()
        # A delivered frame proves the pipeline is healthy again.
        self.consecutive_failures = 0
        return frame

    @asynccontextmanager
    async def session(self):
        await self.acquire()
        try:
            yield self
        finally:
            await self.release()


class CameraManager:
    """Holds the configured cameras keyed by ``camera_num``."""

    def __init__(
        self,
        cameras: dict[int, Camera],
        publishers: dict[int, FramePublisher] | None = None,
    ) -> None:
        self._cameras = cameras
        self._publishers = publishers or {}

    def get(self, camera_num: int) -> Camera:
        return self._cameras[camera_num]

    def publisher(self, camera_num: int) -> FramePublisher:
        return self._publishers[camera_num]

    async def start_publishers(self) -> None:
        for pub in self._publishers.values():
            await pub.start()

    async def stop_publishers(self) -> None:
        for pub in self._publishers.values():
            await pub.stop()

    def attach_failure_callback(
        self, cb: Callable[[int, int], Awaitable[None]]
    ) -> None:
        """Install the persistent-failure notifier on every camera.

        ``cb(camera_num, consecutive_failures)`` is scheduled as a task
        each time a camera is marked broken. The PowerManager uses it
        to decide whether a self power-cycle is warranted.
        """

        for cam in self._cameras.values():
            cam._failure_cb = cb

    def all(self) -> list[Camera]:
        return list(self._cameras.values())

    def numbers(self) -> list[int]:
        return sorted(self._cameras.keys())

    async def shutdown(self) -> None:
        # Per-camera best-effort stop with a hard ceiling. ``picam2.stop()``
        # can block forever against a wedged libcamera pipeline, so we
        # run each one on a daemon thread we're willing to abandon — if
        # it hasn't returned in a few seconds, the python process is
        # about to exit anyway and systemd's KillMode=mixed will SIGKILL
        # whatever's left.
        for cam in self._cameras.values():
            if cam._stop_task is not None and not cam._stop_task.done():
                cam._stop_task.cancel()
            if cam._picam2 is None:
                continue
            done = threading.Event()

            def _runner(c: Camera = cam) -> None:
                try:
                    c._stop_blocking()
                except Exception:
                    logger.exception(
                        "Stop of camera %d failed during shutdown", c.camera_num
                    )
                finally:
                    done.set()

            threading.Thread(
                target=_runner,
                daemon=True,
                name=f"cam{cam.camera_num}-shutdown",
            ).start()
            loop = asyncio.get_running_loop()
            stopped = await loop.run_in_executor(None, done.wait, 3.0)
            if not stopped:
                logger.error(
                    "Camera %d stop hung during shutdown; abandoning",
                    cam.camera_num,
                )

        # Best-effort executor cleanup. Won't kill stuck worker threads
        # (daemon threads die with the process) but tidies up the queue.
        for cam in self._cameras.values():
            cam._picam2_executor.shutdown(wait=False, cancel_futures=True)


def _make_camera(
    num: int, cfg: CameraConfig, stream: StreamConfig, idle_grace_seconds: int
) -> Camera:
    inference_resolution = None
    wildlife = cfg.wildlife
    if wildlife.tile_inference and wildlife.capture_size is not None:
        inference_resolution = tuple(wildlife.capture_size)
    return Camera(
        camera_num=num,
        resolution=cfg.resolution,
        framerate=stream.framerate,
        controls=cfg.controls,
        idle_grace_seconds=idle_grace_seconds,
        inference_resolution=inference_resolution,
    )


def build_manager(config: AppConfig) -> CameraManager:
    cameras = {
        0: _make_camera(
            0, config.camera0, config.stream, config.power.idle_grace_seconds
        ),
        1: _make_camera(
            1, config.camera1, config.stream, config.power.idle_grace_seconds
        ),
    }
    publishers = {
        num: FramePublisher(
            cam,
            framerate=config.stream.framerate,
            jpeg_quality=config.stream.jpeg_quality,
        )
        for num, cam in cameras.items()
    }
    return CameraManager(cameras, publishers)

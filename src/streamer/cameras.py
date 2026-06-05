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
  capture timeout in the MJPEG handler); the next ``acquire()`` rebuilds
  the picamera2 instance from scratch, replacing the per-camera executor
  so the next operation actually has a thread to run on.
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
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from picamera2 import Picamera2  # imported lazily at runtime

from streamer.config import AppConfig, CameraConfig, StreamConfig

logger = logging.getLogger("streamer.cameras")


class Camera:
    """Single picamera2 device with refcounted start/stop."""

    def __init__(
        self,
        camera_num: int,
        resolution: tuple[int, int],
        framerate: float,
        controls: dict[str, Any] | None = None,
        idle_grace_seconds: int = 10,
    ) -> None:
        self.camera_num = camera_num
        self.resolution = resolution
        self.framerate = framerate
        self.controls = dict(controls or {})
        self.idle_grace_seconds = idle_grace_seconds

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
        # Replaced wholesale by ``mark_broken()`` so the next recovery
        # has a free thread even though the previous one is stuck.
        self._picam2_executor: ThreadPoolExecutor = self._make_executor()

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
        rebinds ``_device_lock``. The executor is swapped here so the
        recovery actually has a thread to run on — the previous worker
        is presumed permanently stuck inside libcamera.

        Idempotent (safe to call multiple times) but each call leaks an
        executor + thread, so callers should invoke it at most once per
        detected wedge.
        """

        self._broken = True
        old_exec = self._picam2_executor
        self._picam2_executor = self._make_executor()
        old_exec.shutdown(wait=False, cancel_futures=True)

    async def acquire(self) -> None:
        async with self._refcount_lock:
            self._refcount += 1
            if self._stop_task is not None and not self._stop_task.done():
                self._stop_task.cancel()
                self._stop_task = None
            try:
                if self._broken:
                    logger.warning(
                        "Camera %d marked broken; abandoning instance and reopening",
                        self.camera_num,
                    )
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
        logger.info(
            "Starting camera %d at %dx%d @ %.2f fps",
            self.camera_num,
            width,
            height,
            self.framerate,
        )
        picam2 = Picamera2(camera_num=self.camera_num)
        frame_duration_us = int(round(1_000_000 / self.framerate))
        # ``BGR888`` in picamera2 returns numpy in RGB channel order. Pick
        # this so Pillow's mode="RGB" consumes the array directly with no
        # explicit swap. ``buffer_count=2`` is the minimum that lets the
        # capture loop run smoothly without letting stale buffers
        # accumulate — higher values stalled under the dual-IMX708 setup.
        config = picam2.create_video_configuration(
            main={"size": (width, height), "format": "BGR888"},
            controls={
                "FrameDurationLimits": (frame_duration_us, frame_duration_us),
                **self.controls,
            },
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
        """Capture one frame from the main stream as an RGB (H, W, 3) array."""

        if self._picam2 is None:
            raise RuntimeError(f"Camera {self.camera_num} is not running")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._picam2_executor, self._capture_blocking
        )

    def _capture_blocking(self) -> np.ndarray:
        with self._device_lock:
            picam2 = self._picam2
            if picam2 is None:
                raise RuntimeError(f"Camera {self.camera_num} is not running")
            # Defensive copy: picamera2.capture_array() may, on some
            # libcamera versions / multi-camera setups, hand back a view
            # into a shared buffer pool. Copying decouples the two
            # cameras' frame streams.
            return picam2.capture_array("main").copy()

    @asynccontextmanager
    async def session(self):
        await self.acquire()
        try:
            yield self
        finally:
            await self.release()


class CameraManager:
    """Holds the configured cameras keyed by ``camera_num``."""

    def __init__(self, cameras: dict[int, Camera]) -> None:
        self._cameras = cameras

    def get(self, camera_num: int) -> Camera:
        return self._cameras[camera_num]

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
    return Camera(
        camera_num=num,
        resolution=cfg.resolution,
        framerate=stream.framerate,
        controls=cfg.controls,
        idle_grace_seconds=idle_grace_seconds,
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
    return CameraManager(cameras)

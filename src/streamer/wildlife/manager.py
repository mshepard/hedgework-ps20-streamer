"""Wildlife detection orchestration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from streamer.config import AppConfig, CameraWildlifeConfig
from streamer.wildlife.db import WildlifeDatabase
from streamer.wildlife.detector import (
    SharedHailoContext,
    WildlifeDetectorBackend,
    build_detector,
)
from streamer.wildlife.filters import DetectionFilter
from streamer.wildlife.storage import save_detection_image
from streamer.wildlife.sync import WildlifeSyncWorker
from streamer.wildlife.types import Detection

if TYPE_CHECKING:
    from streamer.cameras import CameraManager
    from streamer.modem import ModemProbe
    from streamer.power import PowerManager

logger = logging.getLogger("streamer.wildlife.manager")


class WildlifeManager:
    def __init__(
        self,
        config: AppConfig,
        cameras: CameraManager,
        state_dir: Path,
        *,
        power: PowerManager | None = None,
        modem: ModemProbe | None = None,
    ) -> None:
        self._config = config
        self._cameras = cameras
        self._state_dir = state_dir
        self._power = power
        self._modem = modem
        self._enabled = config.wildlife.enabled
        self._db = WildlifeDatabase(state_dir / "wildlife.db")
        self._images_dir = state_dir / "detections"
        self._hailo_context = SharedHailoContext()
        self._detectors: dict[int, WildlifeDetectorBackend] = {}
        self._filters: dict[int, DetectionFilter] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._sync: WildlifeSyncWorker | None = None
        self._flush_on_sleep = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def database(self) -> WildlifeDatabase:
        return self._db

    async def start(self) -> None:
        if not self._enabled:
            logger.info("Wildlife detection disabled in config")
            return

        self._images_dir.mkdir(parents=True, exist_ok=True)
        for camera_num in self._cameras.numbers():
            cam_cfg = self._camera_wildlife_config(camera_num)
            model_path = cam_cfg.model_path or self._config.wildlife.model_path
            labels_path = (
                cam_cfg.labels_path or self._config.wildlife.labels_path
            )
            detector = build_detector(
                model_path,
                labels_path,
                tuple(self._config.wildlife.inference_size),
                shared_hailo=self._hailo_context,
            )
            try:
                detector.load()
            except Exception:
                logger.exception(
                    "Failed to load wildlife model for camera %d; using mock",
                    camera_num,
                )
                from streamer.wildlife.detector import MockWildlifeDetector

                detector = MockWildlifeDetector("model load failed")
            self._detectors[camera_num] = detector
            allowed = (
                frozenset(cam_cfg.classes)
                if cam_cfg.classes
                else frozenset()
            )
            self._filters[camera_num] = DetectionFilter(
                confidence_threshold=self._config.wildlife.confidence_threshold,
                cooldown_seconds=self._config.wildlife.cooldown_seconds,
                allowed_classes=allowed,
            )
            if cam_cfg.tile_inference:
                self._tasks.append(
                    asyncio.create_task(
                        self._tiled_detect_loop(camera_num),
                        name=f"wildlife-cam{camera_num}-tiled",
                    )
                )
            else:
                self._tasks.append(
                    asyncio.create_task(
                        self._detect_loop(camera_num),
                        name=f"wildlife-cam{camera_num}",
                    )
                )

        if self._config.wildlife.sync.enabled:
            self._sync = WildlifeSyncWorker(
                self._db,
                wordpress_url=self._config.wildlife.sync.wordpress_url,
                wordpress_user=self._config.wildlife.sync.wordpress_user,
                wordpress_app_password=self._config.wildlife.sync.wordpress_app_password,
                batch_size=self._config.wildlife.sync.batch_size,
                max_uploads_per_hour=self._config.wildlife.sync.max_uploads_per_hour,
                resize_width=self._config.wildlife.sync.resize_width,
                lte_probe=self._lte_ok,
                flush_requested=lambda: self._flush_on_sleep,
            )
            await self._sync.start()

        if self._power is not None:
            self._power.attach_sleep_flush_callback(self._on_entering_sleep)

        logger.info(
            "Wildlife detection started (%d camera loop(s), sync=%s)",
            len(self._tasks),
            self._sync is not None,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._detectors.clear()
        self._hailo_context.close()
        if self._sync is not None:
            await self._sync.stop()
            self._sync = None

    async def _on_entering_sleep(self) -> None:
        self._flush_on_sleep = True
        if self._sync is not None:
            await self._sync.flush_once()
        self._flush_on_sleep = False

    async def _lte_ok(self) -> bool:
        if self._modem is None:
            return True
        snap = self._modem.snapshot()
        return bool(snap and snap.get("reachable"))

    def _camera_wildlife_config(self, camera_num: int) -> CameraWildlifeConfig:
        if camera_num == 0:
            return self._config.camera0.wildlife
        return self._config.camera1.wildlife

    def _active(self) -> bool:
        if self._power is None:
            return True
        from streamer.power import Mode

        return self._power.mode in (Mode.AWAKE, Mode.ENTERING_SLEEP)

    async def _detect_loop(self, camera_num: int) -> None:
        publisher = self._cameras.publisher(camera_num)
        detector = self._detectors[camera_num]
        filt = self._filters[camera_num]
        log = logger.getChild(f"detect.cam{camera_num}")
        last_generation = -1

        while True:
            if not self._active():
                await asyncio.sleep(1.0)
                continue
            try:
                frame = await publisher.wait_frame(last_generation)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Frame wait failed")
                await asyncio.sleep(1.0)
                continue

            last_generation = frame.generation
            try:
                detections = await asyncio.to_thread(
                    detector.detect, frame.rgb
                )
            except Exception:
                log.exception("Inference failed")
                continue

            await self._persist_detections(
                camera_num,
                detections,
                frame.rgb,
                detected_at=datetime.fromtimestamp(
                    frame.captured_at, tz=timezone.utc
                ),
                log=log,
            )

    async def _tiled_detect_loop(self, camera_num: int) -> None:
        """High-res main capture + overlapping tile inference (cam1)."""

        cam = self._cameras.get(camera_num)
        detector = self._detectors[camera_num]
        cam_cfg = self._camera_wildlife_config(camera_num)
        log = logger.getChild(f"detect.cam{camera_num}.tiled")
        interval = cam_cfg.inference_interval_seconds
        grid = tuple(cam_cfg.tile_grid)
        tile_size = tuple(cam_cfg.tile_size)
        overlap = cam_cfg.tile_overlap

        log.info(
            "Tiled inference enabled grid=%sx%s interval=%.1fs capture=%s",
            grid[0],
            grid[1],
            interval,
            cam_cfg.capture_size,
        )

        while True:
            if not self._active():
                await asyncio.sleep(1.0)
                continue
            cycle_started = asyncio.get_running_loop().time()
            try:
                await cam.acquire()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Acquire failed for tiled capture")
                await asyncio.sleep(interval)
                continue
            try:
                try:
                    rgb = await cam.capture_main()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("High-res capture failed")
                    await asyncio.sleep(interval)
                    continue
                try:
                    detections = await asyncio.to_thread(
                        detector.detect_tiled,
                        rgb,
                        grid=grid,
                        tile_size=tile_size,
                        overlap=overlap,
                    )
                except Exception:
                    log.exception("Tiled inference failed")
                    await asyncio.sleep(interval)
                    continue

                await self._persist_detections(
                    camera_num,
                    detections,
                    rgb,
                    detected_at=datetime.now(timezone.utc),
                    log=log,
                )
            finally:
                try:
                    await cam.release()
                except Exception:
                    log.exception("Release after tiled capture failed")

            elapsed = asyncio.get_running_loop().time() - cycle_started
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _persist_detections(
        self,
        camera_num: int,
        detections: list[Detection],
        rgb: np.ndarray,
        *,
        detected_at: datetime,
        log: logging.Logger,
    ) -> None:
        filt = self._filters[camera_num]
        for detection in detections:
            if not filt.accept(camera_num, detection):
                continue
            try:
                image_path = save_detection_image(
                    self._images_dir,
                    camera_num,
                    detection,
                    rgb,
                    store_annotated=self._config.wildlife.store_annotated,
                    detected_at=detected_at,
                )
            except Exception:
                log.exception("Failed to save detection image")
                continue

            conn = await self._db.connect()
            try:
                row_id = await self._db.insert_detection(
                    conn,
                    detected_at=detected_at,
                    camera=camera_num,
                    species=detection.species,
                    display_name=detection.display_name,
                    confidence=detection.confidence,
                    bbox=(
                        detection.x_center,
                        detection.y_center,
                        detection.width,
                        detection.height,
                    ),
                    image_path=image_path,
                )
            finally:
                await conn.close()
            log.info(
                "Saved detection id=%d camera=%d species=%s conf=%.2f",
                row_id,
                camera_num,
                detection.species,
                detection.confidence,
            )

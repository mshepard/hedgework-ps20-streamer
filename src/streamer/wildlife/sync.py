"""Upload detections to the WordPress Wildlife Watch plugin."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from streamer.wildlife.db import WildlifeDatabase
from streamer.wildlife.storage import resize_for_upload

logger = logging.getLogger("streamer.wildlife.sync")

SYNC_INTERVAL_SECONDS = 300.0


class WildlifeSyncWorker:
    def __init__(
        self,
        db: WildlifeDatabase,
        *,
        wordpress_url: str,
        wordpress_user: str,
        wordpress_app_password: str,
        batch_size: int,
        max_uploads_per_hour: int,
        resize_width: int,
        lte_probe: Callable[[], Awaitable[bool]] | None = None,
        flush_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._db = db
        self._wordpress_url = wordpress_url.rstrip("/")
        self._wordpress_user = wordpress_user
        self._wordpress_app_password = wordpress_app_password
        self._batch_size = batch_size
        self._max_uploads_per_hour = max_uploads_per_hour
        self._resize_width = resize_width
        self._lte_probe = lte_probe
        self._flush_requested = flush_requested
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._upload_timestamps: list[float] = []

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._loop(), name="wildlife-sync"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.flush_once()

    async def flush_once(self) -> int:
        """Upload one batch immediately (e.g. before sleep)."""

        return await self._sync_batch(force=True)

    async def _loop(self) -> None:
        while self._running:
            try:
                if self._flush_requested and self._flush_requested():
                    await self._sync_batch(force=True)
                elif await self._can_upload():
                    await self._sync_batch(force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Wildlife sync loop error")
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)

    async def _can_upload(self) -> bool:
        if self._lte_probe is not None:
            return await self._lte_probe()
        return True

    def _within_hourly_cap(self) -> bool:
        now = time.time()
        hour_ago = now - 3600.0
        self._upload_timestamps = [
            ts for ts in self._upload_timestamps if ts >= hour_ago
        ]
        return len(self._upload_timestamps) < self._max_uploads_per_hour

    async def _sync_batch(self, *, force: bool) -> int:
        if not self._wordpress_url or not self._wordpress_user:
            return 0
        if not force and not self._within_hourly_cap():
            logger.debug("Wildlife sync hourly cap reached")
            return 0
        if not force and not await self._can_upload():
            logger.debug("Wildlife sync skipped: LTE probe failed")
            return 0

        conn = await self._db.connect()
        try:
            rows = await self._db.unsynced(conn, limit=self._batch_size)
        finally:
            await conn.close()

        uploaded = 0
        for row in rows:
            if not force and not self._within_hourly_cap():
                break
            try:
                media_id = await self._upload_row(row)
            except Exception:
                logger.exception(
                    "Failed to sync detection %s to WordPress", row["id"]
                )
                continue
            conn = await self._db.connect()
            try:
                await self._db.mark_synced(
                    conn, int(row["id"]), wp_media_id=media_id
                )
            finally:
                await conn.close()
            self._upload_timestamps.append(time.time())
            uploaded += 1
        if uploaded:
            logger.info("Synced %d wildlife detection(s) to WordPress", uploaded)
        return uploaded

    async def _upload_row(self, row: dict[str, Any]) -> int | None:
        image_path = Path(row["image_path"])
        if not image_path.is_file():
            raise FileNotFoundError(image_path)

        image_bytes = resize_for_upload(image_path, self._resize_width)
        auth = base64.b64encode(
            f"{self._wordpress_user}:{self._wordpress_app_password}".encode()
        ).decode("ascii")
        headers = {"Authorization": f"Basic {auth}"}

        payload = {
            "detected_at": row["detected_at"],
            "camera": row["camera"],
            "species": row["species"],
            "display_name": row["display_name"],
            "confidence": row["confidence"],
            "bbox": {
                "x": row["bbox_x"],
                "y": row["bbox_y"],
                "w": row["bbox_w"],
                "h": row["bbox_h"],
            },
        }

        form = aiohttp.FormData()
        form.add_field(
            "image",
            image_bytes,
            filename=image_path.name,
            content_type="image/jpeg",
        )
        form.add_field("metadata", json.dumps(payload), content_type="application/json")

        url = f"{self._wordpress_url}/wp-json/hedgework/v1/sighting"
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=form, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(
                        f"WordPress sync failed ({resp.status}): {body!r}"
                    )
                media_id = body.get("media_id") if isinstance(body, dict) else None
                return int(media_id) if media_id is not None else None

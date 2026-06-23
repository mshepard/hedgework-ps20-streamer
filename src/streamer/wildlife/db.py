"""SQLite persistence for wildlife detections."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at   TEXT NOT NULL,
    camera        INTEGER NOT NULL,
    species       TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    confidence    REAL NOT NULL,
    bbox_x        REAL NOT NULL,
    bbox_y        REAL NOT NULL,
    bbox_w        REAL NOT NULL,
    bbox_h        REAL NOT NULL,
    image_path    TEXT NOT NULL,
    synced_at     TEXT,
    wp_media_id   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_detections_sync ON detections(synced_at);
CREATE INDEX IF NOT EXISTS idx_detections_species_day
    ON detections(species, detected_at);
"""


class WildlifeDatabase:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @property
    def path(self) -> Path:
        return self._db_path

    async def connect(self) -> aiosqlite.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        return conn

    async def insert_detection(
        self,
        conn: aiosqlite.Connection,
        *,
        detected_at: datetime,
        camera: int,
        species: str,
        display_name: str,
        confidence: float,
        bbox: tuple[float, float, float, float],
        image_path: Path,
    ) -> int:
        cursor = await conn.execute(
            """
            INSERT INTO detections (
                detected_at, camera, species, display_name, confidence,
                bbox_x, bbox_y, bbox_w, bbox_h, image_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                detected_at.astimezone(timezone.utc).isoformat(),
                camera,
                species,
                display_name,
                confidence,
                bbox[0],
                bbox[1],
                bbox[2],
                bbox[3],
                str(image_path),
            ),
        )
        await conn.commit()
        return int(cursor.lastrowid)

    async def recent(
        self, conn: aiosqlite.Connection, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        cursor = await conn.execute(
            """
            SELECT * FROM detections
            ORDER BY detected_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def counts_today(
        self, conn: aiosqlite.Connection
    ) -> list[dict[str, Any]]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await conn.execute(
            """
            SELECT species, display_name, COUNT(*) AS count
            FROM detections
            WHERE detected_at LIKE ?
            GROUP BY species, display_name
            ORDER BY count DESC, display_name ASC
            """,
            (f"{today}%",),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def unsynced(
        self, conn: aiosqlite.Connection, *, limit: int
    ) -> list[dict[str, Any]]:
        cursor = await conn.execute(
            """
            SELECT * FROM detections
            WHERE synced_at IS NULL
            ORDER BY detected_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_synced(
        self,
        conn: aiosqlite.Connection,
        detection_id: int,
        *,
        wp_media_id: int | None,
    ) -> None:
        await conn.execute(
            """
            UPDATE detections
            SET synced_at = ?, wp_media_id = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                wp_media_id,
                detection_id,
            ),
        )
        await conn.commit()

    async def get_by_id(
        self, conn: aiosqlite.Connection, detection_id: int
    ) -> dict[str, Any] | None:
        cursor = await conn.execute(
            "SELECT * FROM detections WHERE id = ?", (detection_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    @staticmethod
    def row_to_json(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
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
            "image_path": row["image_path"],
            "synced_at": row["synced_at"],
            "wp_media_id": row["wp_media_id"],
        }

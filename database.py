"""SQLite-backed discovery cache for Mapillary images."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import GPS_COORD_PRECISION


class DiscoveryDB:
    """SQLite discovery cache — singleton per db_path."""

    instances: dict[str, "DiscoveryDB"] = {}

    CREATE_IMAGES = """
        CREATE TABLE IF NOT EXISTS images (
            id            TEXT PRIMARY KEY,
            lat           REAL NOT NULL,
            lon           REAL NOT NULL,
            downloaded    INTEGER NOT NULL DEFAULT 0,
            discovered_at INTEGER NOT NULL,
            downloaded_at INTEGER
        )
    """
    CREATE_META = """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """

    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute(self.CREATE_IMAGES)
        self.conn.execute(self.CREATE_META)
        self.conn.commit()

    @classmethod
    def get(cls, db_path: Path) -> "DiscoveryDB":
        key = str(db_path)
        if key not in cls.instances:
            cls.instances[key] = cls(db_path)
        return cls.instances[key]

    def insert_images(self, images: list[dict]) -> None:
        """Bulk insert images (API format with geometry.coordinates), ignoring duplicates."""
        now = int(datetime.now(timezone.utc).timestamp())
        rows = []
        for img in images:
            img_id = img.get("id")
            geometry = img.get("geometry", {})
            coords = geometry.get("coordinates", []) if geometry else []
            if not img_id or len(coords) < 2:
                continue
            lat = round(coords[1] * GPS_COORD_PRECISION) / GPS_COORD_PRECISION
            lon = round(coords[0] * GPS_COORD_PRECISION) / GPS_COORD_PRECISION
            rows.append((img_id, lat, lon, now))
        self.conn.executemany(
            "INSERT OR IGNORE INTO images (id, lat, lon, discovered_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def upsert_downloaded(self, image_id: str, lat: float, lon: float) -> None:
        """Insert image if not present, then mark as downloaded."""
        self.insert_images([{"id": image_id, "geometry": {"coordinates": [lon, lat]}}])
        self.mark_downloaded(image_id)

    def mark_downloaded(self, image_id: str) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        self.conn.execute(
            "UPDATE images SET downloaded=1, downloaded_at=? WHERE id=?",
            (now, image_id),
        )
        self.conn.commit()

    def get_pending_images_metadata(self) -> list[dict]:
        cursor = self.conn.execute("SELECT id, lat, lon FROM images WHERE downloaded=0")
        return [{"id": r[0], "lat": r[1], "lon": r[2]} for r in cursor.fetchall()]

    def get_pending_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM images WHERE downloaded=0")
        return cursor.fetchone()[0]

    def get_downloaded_ids(self) -> set[str]:
        cursor = self.conn.execute("SELECT id FROM images WHERE downloaded=1")
        return {r[0] for r in cursor.fetchall()}

    def get_image_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM images")
        return cursor.fetchone()[0]

    def get_last_discovered_at(self) -> Optional[datetime]:
        value = self.get_meta("last_discovered_at")
        if value is None:
            return None
        return datetime.fromtimestamp(int(value), tz=timezone.utc)

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        cursor = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def wipe_images(self) -> None:
        """Delete all rows from images table (for rediscover state)."""
        self.conn.execute("DELETE FROM images")
        self.conn.commit()

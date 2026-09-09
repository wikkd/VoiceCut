"""Media registry: SQLite-backed item store.

Each entry corresponds to one editable audio track (plus optional video preview).
Items persist across restarts (workdir/voicecut.db); every mutation is committed
immediately. IDs are stable uuids (m-<hex>).
"""
from __future__ import annotations

import itertools
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app import db


@dataclass
class MediaItem:
    """One editable media item.

    - wav_path    : internal working WAV (48k mono PCM); waveform/playback use it
    - preview_mp4 : optional browser video preview file
    - source      : original source path or URL
    - derived_from: parent item id (denoise/separation/... artifacts)
    """

    id: str
    name: str
    wav_path: Path
    duration: float
    sample_rate: int
    source: str = ""
    preview_mp4: Path | None = None
    derived_from: str | None = None
    kind: str = "audio"          # audio | video | vocal | instrumental | denoised | url | ...
    created: float = field(default_factory=lambda: time.time())
    extra: dict = field(default_factory=dict)


def _to_row(item: MediaItem) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "kind": item.kind,
        "wav_path": str(item.wav_path),
        "preview_mp4": str(item.preview_mp4) if item.preview_mp4 else None,
        "source": item.source,
        "derived_from": item.derived_from,
        "duration": float(item.duration),
        "sample_rate": int(item.sample_rate),
        "created": float(item.created),
        "extra": json.dumps(item.extra, ensure_ascii=False),
    }


def _from_row(row: dict) -> MediaItem:
    return MediaItem(
        id=row["id"],
        name=row["name"],
        wav_path=Path(row["wav_path"]),
        duration=float(row["duration"]),
        sample_rate=int(row["sample_rate"]),
        source=row["source"] or "",
        preview_mp4=Path(row["preview_mp4"]) if row.get("preview_mp4") else None,
        derived_from=row.get("derived_from"),
        kind=row["kind"] or "audio",
        created=float(row["created"]),
        extra=json.loads(row["extra"] or "{}"),
    )


class MediaStore:
    """Thread-safe SQLite-backed media registry."""

    def __init__(self, workdir: str | Path) -> None:
        self._conn = db.get_conn(workdir)
        self._lock = threading.RLock()
        self._items: dict[str, MediaItem] = {}
        for row in db.fetch_all_items(self._conn):
            item = _from_row(row)
            self._items[item.id] = item

    def add(self, item: MediaItem) -> MediaItem:
        with self._lock:
            self._items[item.id] = item
            db.insert_item(self._conn, _to_row(item))
        return item

    def get(self, item_id: str) -> MediaItem | None:
        with self._lock:
            return self._items.get(item_id)

    def require(self, item_id: str) -> MediaItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"item not found: {item_id}")
        return item

    def all(self) -> list[MediaItem]:
        with self._lock:
            return list(self._items.values())

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items.pop(item_id, None)
            db.delete_item_row(self._conn, item_id)  # cascades projects

    def persist(self, item: MediaItem) -> None:
        """Commit in-place mutations (extra / name changes)."""
        with self._lock:
            self._items[item.id] = item
            db.insert_item(self._conn, _to_row(item))

    def new_id(self) -> str:
        """Stable, collision-free item id."""
        return f"m-{uuid.uuid4().hex[:10]}"

    def unique_name(self, base: str) -> str:
        """Generate a non-colliding item name (append numeric suffix)."""
        with self._lock:
            names = {i.name for i in self._items.values()}
        if base not in names:
            return base
        for n in itertools.count(1):
            cand = f"{base}_{n}"
            if cand not in names:
                return cand
"""SQLite storage layer: media registry + per-item project snapshots.

Single file per workdir: workdir/voicecut.db (WAL mode, stdlib sqlite3).
- items    : media item metadata; ``extra`` is a JSON blob (peaks, subs_file, ...)
- projects : per-item character pool / segments / speaker_segments (JSON blob)

A one-time best-effort migration imports the legacy JSON layout
(workdir/items/*.wav + workdir/projects/*.json) when the DB is created empty.
Legacy mapping rule: a wav file m00N.wav belonged to legacy item id m00(N+1)
(an old double-new_id bug); preview/srt share the wav base name.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

_db_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'audio',
    wav_path     TEXT NOT NULL,
    preview_mp4  TEXT,
    source       TEXT,
    derived_from TEXT,
    duration     REAL NOT NULL DEFAULT 0,
    sample_rate  INTEGER NOT NULL DEFAULT 48000,
    created      REAL NOT NULL,
    extra        TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS projects (
    item_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL DEFAULT '{}'
);
"""

_ITEM_COLS = ("id", "name", "kind", "wav_path", "preview_mp4", "source",
              "derived_from", "duration", "sample_rate", "created", "extra")


def db_path(workdir: str | Path) -> Path:
    return Path(workdir) / "voicecut.db"


def get_conn(workdir: str | Path) -> sqlite3.Connection:
    """Get (and cache) a thread-safe connection for a workdir."""
    key = str(Path(workdir).resolve())
    with _db_lock:
        conn = _conns.get(key)
        if conn is None:
            conn = _connect(key)
            _conns[key] = conn
            migrate_legacy(Path(key), conn)
        return conn


def _connect(key: str) -> sqlite3.Connection:
    path = Path(key) / "voicecut.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with _db_lock:
        conn.executescript(SCHEMA)
        conn.commit()
    return conn


def reset_conns() -> None:
    """Close all cached connections (test helper)."""
    global _conns
    with _db_lock:
        for c in _conns.values():
            try:
                c.close()
            except Exception:
                pass
        _conns = {}


# ── items ────────────────────────────────────────────────────

def count_items(conn: sqlite3.Connection) -> int:
    with _db_lock:
        return int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


def fetch_item(conn: sqlite3.Connection, item_id: str) -> dict | None:
    with _db_lock:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None


def fetch_all_items(conn: sqlite3.Connection) -> list[dict]:
    with _db_lock:
        return [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY created")]


def insert_item(conn: sqlite3.Connection, row: dict) -> None:
    with _db_lock:
        conn.execute(
            "INSERT OR REPLACE INTO items (%s) "
            "VALUES (%s)" % (",".join(_ITEM_COLS), ",".join(":" + c for c in _ITEM_COLS)),
            row)
        conn.commit()


def delete_item_row(conn: sqlite3.Connection, item_id: str) -> None:
    with _db_lock:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.execute("DELETE FROM projects WHERE item_id=?", (item_id,))
        conn.commit()


# ── projects ─────────────────────────────────────────────────

def fetch_project(conn: sqlite3.Connection, item_id: str) -> str | None:
    with _db_lock:
        row = conn.execute("SELECT data FROM projects WHERE item_id=?", (item_id,)).fetchone()
        return row["data"] if row else None


def upsert_project(conn: sqlite3.Connection, item_id: str, data: str) -> None:
    with _db_lock:
        conn.execute(
            "INSERT INTO projects (item_id, data) VALUES (?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET data=excluded.data",
            (item_id, data))
        conn.commit()


def delete_project_row(conn: sqlite3.Connection, item_id: str) -> None:
    with _db_lock:
        conn.execute("DELETE FROM projects WHERE item_id=?", (item_id,))
        conn.commit()


# ── legacy migration ────────────────────────────────────────

def _legacy_num(stem: str) -> int | None:
    if not (stem.startswith("m") and stem[1:].isdigit()):
        return None
    try:
        return int(stem[1:])
    except ValueError:
        return None


def migrate_legacy(workdir: Path, conn: sqlite3.Connection) -> None:
    """One-time best-effort import of the legacy JSON layout (empty DB only)."""
    with _db_lock:
        if count_items(conn) > 0:
            return
        items_dir = workdir / "items"
        if not items_dir.is_dir():
            return
        wavs = sorted(items_dir.glob("*.wav"))
        if not wavs:
            return
        from app.ffmpeg_util import media_duration  # lazy: needs ffmpeg
        rows: list[dict[str, Any]] = []
        projs: list[tuple[str, str]] = []
        for wav in wavs:
            num = _legacy_num(wav.stem)
            if num is None:
                continue
            new_id = f"m-{uuid.uuid4().hex[:10]}"
            duration = 0.0
            try:
                duration = float(media_duration(wav) or 0)
            except Exception:
                pass
            extra: dict[str, Any] = {}
            preview = items_dir / f"{wav.stem}.preview.mp4"
            if not preview.exists():
                preview = None
            srt = items_dir / f"{wav.stem}.srt"
            if srt.exists():
                extra["subs_file"] = str(srt)
            rows.append({
                "id": new_id, "name": wav.stem, "kind": "audio",
                "wav_path": str(wav),
                "preview_mp4": str(preview) if preview else None,
                "source": "", "derived_from": None,
                "duration": duration, "sample_rate": 48000,
                "created": wav.stat().st_mtime,
                "extra": json.dumps(extra, ensure_ascii=False),
            })
            legacy_item = f"m{num + 1:04d}"
            proj_file = workdir / "projects" / f"{legacy_item}.json"
            if proj_file.exists():
                try:
                    data = json.loads(proj_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        data.setdefault("characters", [])
                        data.setdefault("segments", [])
                        data.setdefault("speaker_segments", [])
                        projs.append((new_id, json.dumps(data, ensure_ascii=False)))
                except Exception:
                    pass
        if not rows:
            return
        try:
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO items (%s) VALUES (%s)"
                % (",".join(_ITEM_COLS), ",".join(":" + c for c in _ITEM_COLS)),
                rows)
            conn.executemany("INSERT INTO projects (item_id, data) VALUES (?, ?)", projs)
            conn.commit()
        except Exception:
            conn.rollback()
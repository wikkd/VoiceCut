"""SQLite storage layer: projects + media registry + per-item snapshots.

Single file per workdir: workdir/voicecut.db (WAL mode, stdlib sqlite3).
- projects     : project records; ``extra`` holds the project-level character
                 pool as ``{"characters": [...]}`` (characters may carry a
                 base64 ``embedding`` + ``emb_count`` representative voiceprint).
- items        : media item metadata; ``extra`` is a JSON blob (peaks, subs_file, ...).
- item_projects: per-item segments / speaker_segments (JSON blob).

v1 -> v2 migration (idempotent, runs at first connect): the old per-item
``projects`` table (item_id -> data) is renamed to ``item_projects``; its
``characters`` are merged into a project-level pool with labels namespaced as
"<item_id>:<label>"; items gain a ``project_id`` column and are attached to a
default project.

A one-time best-effort legacy import (empty DB only) reads the pre-SQLite JSON
layout (workdir/items/*.wav + workdir/projects/*.json). Mapping rule: a wav file
m00N.wav belonged to legacy item id m00(N+1) (an old double-new_id bug);
preview/srt share the wav base name.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_db_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    extra   TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    project_id   TEXT,
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
CREATE TABLE IF NOT EXISTS item_projects (
    item_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL DEFAULT '{}'
);
"""

_PROJECT_COLS = ("id", "name", "created", "updated", "extra")
_ITEM_COLS = ("id", "project_id", "name", "kind", "wav_path", "preview_mp4", "source",
              "derived_from", "duration", "sample_rate", "created", "extra")

DEFAULT_PROJECT_NAME = "默认项目"  # default project name


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
            _init_db(Path(key), conn)
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


def _init_db(workdir: Path, conn: sqlite3.Connection) -> None:
    """Idempotent bootstrap: v1->v2 migration, default project, legacy import."""
    with _db_lock:
        _migrate_v1(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_project ON items(project_id)")
        conn.commit()
        ensure_default_project(conn)
    migrate_legacy(workdir, conn)


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

def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def _table_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Rename old per-item ``projects`` -> ``item_projects`` and pool characters."""
    tables = _table_names(conn)
    proj_cols = _table_cols(conn, "projects") if "projects" in tables else []
    if not proj_cols or "item_id" not in proj_cols:
        return  # already v2 (projects holds project records)
    if "item_projects" in tables:
        n = int(conn.execute("SELECT COUNT(*) FROM item_projects").fetchone()[0])
        if n:
            return  # conflicting state; leave as-is rather than drop data
        conn.execute("DROP TABLE item_projects")
    conn.execute("ALTER TABLE projects RENAME TO item_projects")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS projects ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL,"
        " created REAL NOT NULL, updated REAL NOT NULL,"
        " extra TEXT NOT NULL DEFAULT '{}')")
    if "project_id" not in _table_cols(conn, "items"):
        conn.execute("ALTER TABLE items ADD COLUMN project_id TEXT")
    conn.commit()
    _pool_characters_from_item_data(conn)


def _pool_characters_from_item_data(conn: sqlite3.Connection) -> None:
    """Move per-item characters into a project pool (labels namespaced by item)."""
    rows = conn.execute("SELECT item_id, data FROM item_projects").fetchall()
    pool: list[dict[str, Any]] = []
    updated: list[tuple[str, str]] = []
    for r in rows:
        item_id, raw = r["item_id"], r["data"]
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        chars = data.pop("characters", None)
        if isinstance(chars, list) and chars:
            for c in chars:
                if not isinstance(c, dict) or not c.get("id"):
                    continue
                c = dict(c)
                c["speakerLabels"] = [f"{item_id}:{lb}" for lb in (c.get("speakerLabels") or [])]
                pool.append(c)
            updated.append((item_id, json.dumps(data, ensure_ascii=False, indent=1)))
    if updated:
        for item_id, data in updated:
            conn.execute(
                "INSERT INTO item_projects (item_id, data) VALUES (?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET data=excluded.data",
                (item_id, data))
    if pool:
        pid = ensure_default_project(conn)
        rec = fetch_project_record(conn, pid) or {"extra": "{}"}
        try:
            extra = json.loads(rec["extra"] or "{}")
        except Exception:
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        extra["characters"] = pool
        conn.execute(
            "UPDATE projects SET extra=?, updated=? WHERE id=?",
            (json.dumps(extra, ensure_ascii=False), time.time(), pid))
    conn.commit()


# ---- projects ---------------------------------------------------

def ensure_default_project(conn: sqlite3.Connection) -> str:
    with _db_lock:
        row = conn.execute("SELECT id FROM projects ORDER BY created LIMIT 1").fetchone()
        if row:
            return row["id"]
        pid = f"p-{uuid.uuid4().hex[:10]}"
        now = time.time()
        conn.execute(
            "INSERT INTO projects (id, name, created, updated, extra) VALUES (?,?,?,?,?)",
            (pid, DEFAULT_PROJECT_NAME, now, now, "{}"))
        conn.execute("UPDATE items SET project_id=? WHERE project_id IS NULL", (pid,))
        conn.commit()
        return pid


def fetch_project_records(conn: sqlite3.Connection) -> list[dict]:
    with _db_lock:
        return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY created")]


def fetch_project_record(conn: sqlite3.Connection, project_id: str) -> dict | None:
    with _db_lock:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None


def insert_project(conn: sqlite3.Connection, project_id: str, name: str,
                   created: float, updated: float, extra: dict) -> None:
    with _db_lock:
        conn.execute(
            "INSERT INTO projects (id, name, created, updated, extra) VALUES (?,?,?,?,?)",
            (project_id, name, created, updated, json.dumps(extra or {}, ensure_ascii=False)))
        conn.commit()


def rename_project_record(conn: sqlite3.Connection, project_id: str, name: str) -> None:
    with _db_lock:
        conn.execute("UPDATE projects SET name=?, updated=? WHERE id=?", (name, time.time(), project_id))
        conn.commit()


def delete_project_record(conn: sqlite3.Connection, project_id: str) -> None:
    with _db_lock:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.commit()


def update_project_extra(conn: sqlite3.Connection, project_id: str, extra: dict) -> None:
    with _db_lock:
        conn.execute(
            "UPDATE projects SET extra=?, updated=? WHERE id=?",
            (json.dumps(extra or {}, ensure_ascii=False), time.time(), project_id))
        conn.commit()


def count_items_in_project(conn: sqlite3.Connection, project_id: str) -> int:
    with _db_lock:
        return int(conn.execute(
            "SELECT COUNT(*) FROM items WHERE project_id=?", (project_id,)).fetchone()[0])

# ---- items ------------------------------------------------------

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


def fetch_items_by_project(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    with _db_lock:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM items WHERE project_id=? ORDER BY created", (project_id,))]


def insert_item(conn: sqlite3.Connection, row: dict) -> None:
    with _db_lock:
        conn.execute(
            "INSERT OR REPLACE INTO items (%s) VALUES (%s)"
            % (",".join(_ITEM_COLS), ",".join(":" + c for c in _ITEM_COLS)), row)
        conn.commit()


def update_item_project(conn: sqlite3.Connection, item_id: str, project_id: str) -> None:
    with _db_lock:
        conn.execute("UPDATE items SET project_id=? WHERE id=?", (project_id, item_id))
        conn.commit()


def delete_item_row(conn: sqlite3.Connection, item_id: str) -> None:
    with _db_lock:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.execute("DELETE FROM item_projects WHERE item_id=?", (item_id,))
        conn.commit()


# ---- item_projects (per-item snapshots) -------------------------

def fetch_project(conn: sqlite3.Connection, item_id: str) -> str | None:
    with _db_lock:
        row = conn.execute(
            "SELECT data FROM item_projects WHERE item_id=?", (item_id,)).fetchone()
        return row["data"] if row else None


def upsert_project(conn: sqlite3.Connection, item_id: str, data: str) -> None:
    with _db_lock:
        conn.execute(
            "INSERT INTO item_projects (item_id, data) VALUES (?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET data=excluded.data",
            (item_id, data))
        conn.commit()


def delete_project_row(conn: sqlite3.Connection, item_id: str) -> None:
    with _db_lock:
        conn.execute("DELETE FROM item_projects WHERE item_id=?", (item_id,))
        conn.commit()


# ---- legacy migration (pre-SQLite JSON layout) -------------------

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
        pool: list[dict[str, Any]] = []
        pid = ensure_default_project(conn)
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
                "id": new_id, "project_id": pid, "name": wav.stem, "kind": "audio",
                "wav_path": str(wav), "preview_mp4": str(preview) if preview else None,
                "source": "", "derived_from": None, "duration": duration,
                "sample_rate": 48000, "created": wav.stat().st_mtime,
                "extra": json.dumps(extra, ensure_ascii=False),
            })
            legacy_item = f"m{num + 1:04d}"
            proj_file = workdir / "projects" / f"{legacy_item}.json"
            if proj_file.exists():
                try:
                    data = json.loads(proj_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        chars = data.get("characters") or []
                        if isinstance(chars, list):
                            for c in chars:
                                if isinstance(c, dict) and c.get("id"):
                                    c = dict(c)
                                    c["speakerLabels"] = [
                                        f"{new_id}:{lb}" for lb in (c.get("speakerLabels") or [])]
                                    pool.append(c)
                        data.pop("characters", None)
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
                % (",".join(_ITEM_COLS), ",".join(":" + c for c in _ITEM_COLS)), rows)
            conn.executemany("INSERT INTO item_projects (item_id, data) VALUES (?, ?)", projs)
            if pool:
                rec = fetch_project_record(conn, pid) or {"extra": "{}"}
                try:
                    extra = json.loads(rec["extra"] or "{}")
                except Exception:
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                extra["characters"] = pool
                conn.execute(
                    "UPDATE projects SET extra=?, updated=? WHERE id=?",
                    (json.dumps(extra, ensure_ascii=False), time.time(), pid))
            conn.commit()
        except Exception:
            conn.rollback()

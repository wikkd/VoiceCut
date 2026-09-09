"""Per-item project state persistence (characters, segments, speaker segments).

Stored in the SQLite DB (workdir/voicecut.db, table ``projects``) so the
character pool / segments / speaker labels survive page refresh and restarts.
The legacy JSON files under workdir/projects/ are migrated once by app.db.

Client-facing data shapes (plain dicts):
  Character        = {id, name, color, speakerLabels: [...], created}
  Segment          = {id, start, end, text, language, speakerLabel, characterId, note?}
  speaker_segment  = {start, end, label}
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app import db

PROJECT_VERSION = 1

# Auto character palette (distinct hues on dark UI)
PALETTE = [
    "#e5484d", "#f76808", "#f5d90a", "#46a758", "#3e63dd",
    "#8e4ec6", "#12a594", "#e93d82", "#00a2c7", "#ffb224",
]

_color_state = {"n": 0}


def new_uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def next_color() -> str:
    c = PALETTE[_color_state["n"] % len(PALETTE)]
    _color_state["n"] += 1
    return c


def default_project() -> dict:
    return {
        "version": PROJECT_VERSION,
        "characters": [],
        "segments": [],
        "speaker_segments": [],
    }


def project_path(workdir: Path, item_id: str) -> Path:
    """Legacy JSON path (unused after migration; kept for back-compat/tests)."""
    return Path(workdir) / "projects" / f"{item_id}.json"


def load_project(workdir: Path, item_id: str) -> dict:
    conn = db.get_conn(workdir)
    raw = db.fetch_project(conn, item_id)
    if raw is None:
        return default_project()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return default_project()
        data.setdefault("characters", [])
        data.setdefault("segments", [])
        data.setdefault("speaker_segments", [])
        return data
    except Exception:
        return default_project()


def save_project(workdir: Path, item_id: str, data: dict) -> dict:
    conn = db.get_conn(workdir)
    db.upsert_project(conn, item_id, json.dumps(data, ensure_ascii=False, indent=1))
    return data


def delete_project(workdir: Path, item_id: str) -> None:
    conn = db.get_conn(workdir)
    db.delete_project_row(conn, item_id)
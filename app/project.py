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


def _json_list(raw: str | None, fallback: list) -> list:
    """解析 JSON 列表列；空/损坏时回退到 fallback。"""
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return fallback


def load_project(workdir: Path, item_id: str) -> dict:
    conn = db.get_conn(workdir)
    cols = db.fetch_project_columns(conn, item_id)
    if cols is None:
        return default_project()
    data_raw, seg_raw, spk_raw = cols
    try:
        data = json.loads(data_raw or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("characters", [])
    data["segments"] = _json_list(seg_raw, data.get("segments") or [])
    data["speaker_segments"] = _json_list(spk_raw, data.get("speaker_segments") or [])
    return data


def save_project(workdir: Path, item_id: str, data: dict) -> dict:
    """持久化 per-item 项目状态。

    segments / speaker_segments 写入独立列（紧凑 JSON），其余字段
    （characters / version / ...）写入 data 列，避免每次全包序列化。
    """
    conn = db.get_conn(workdir)
    segs = data.get("segments") or []
    spks = data.get("speaker_segments") or []
    rest = {k: v for k, v in data.items() if k not in ("segments", "speaker_segments")}
    db.upsert_project(
        conn, item_id,
        json.dumps(rest, ensure_ascii=False),
        segments=json.dumps(segs, ensure_ascii=False),
        speaker_segments=json.dumps(spks, ensure_ascii=False),
    )
    return data


def delete_project(workdir: Path, item_id: str) -> None:
    conn = db.get_conn(workdir)
    db.delete_project_row(conn, item_id)

# ---- project-level shared character pool -------------------------

def default_pool() -> dict:
    """Empty project character pool."""
    return {"characters": []}


def load_pool(workdir: Path, project_id: str) -> dict:
    """Load the project-level shared character pool (empty dict on missing)."""
    conn = db.get_conn(workdir)
    rec = db.fetch_project_record(conn, project_id)
    if rec is None:
        return default_pool()
    try:
        extra = json.loads(rec["extra"] or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    chars = extra.get("characters")
    if not isinstance(chars, list):
        chars = []
    return {"characters": chars}


def save_pool(workdir: Path, project_id: str, characters: list) -> dict:
    """Persist the project-level character pool; keeps other project settings.

    Returns {"characters": [...]}.
    """
    conn = db.get_conn(workdir)
    chars = list(characters or [])
    rec = db.fetch_project_record(conn, project_id)
    try:
        extra = json.loads(rec["extra"] or "{}") if rec else {}
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["characters"] = chars
    db.update_project_extra(conn, project_id, extra)
    return {"characters": chars}


def _load_extra(workdir: Path, project_id: str) -> dict:
    """Read the project record's raw extra dict (characters + settings)."""
    conn = db.get_conn(workdir)
    rec = db.fetch_project_record(conn, project_id)
    if rec is None:
        return {}
    try:
        extra = json.loads(rec["extra"] or "{}")
    except Exception:
        extra = {}
    return extra if isinstance(extra, dict) else {}


def get_project_setting(workdir: Path, project_id: str, key: str, default=None):
    """Read one project-level setting (e.g. auto_analyze) from the extra blob."""
    return _load_extra(workdir, project_id).get(key, default)


def set_project_setting(workdir: Path, project_id: str, key: str, value) -> None:
    """Persist one project-level setting, preserving the character pool."""
    conn = db.get_conn(workdir)
    rec = db.fetch_project_record(conn, project_id)
    if rec is None:
        return
    try:
        extra = json.loads(rec["extra"] or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra[key] = value
    db.update_project_extra(conn, project_id, extra)

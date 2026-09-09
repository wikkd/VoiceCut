"""Per-item project state persistence (characters, segments, speaker segments).

Stored as JSON under workdir/projects/<item_id>.json so the character pool,
segments and speaker labels survive page refresh / app restart.
Data shapes (all client-facing, stored as plain dicts):
  Character  = {id, name, color, speakerLabels: [..], created}
  Segment    = {id, start, end, text, language, speakerLabel, characterId, note?}
  speaker_segment = {start, end, label}
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

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
    return Path(workdir) / "projects" / f"{item_id}.json"


def load_project(workdir: Path, item_id: str) -> dict:
    p = project_path(workdir, item_id)
    if not p.exists():
        return default_project()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_project()
        data.setdefault("characters", [])
        data.setdefault("segments", [])
        data.setdefault("speaker_segments", [])
        return data
    except Exception:
        return default_project()


def save_project(workdir: Path, item_id: str, data: dict) -> dict:
    p = project_path(workdir, item_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def delete_project(workdir: Path, item_id: str) -> None:
    try:
        p = project_path(workdir, item_id)
        if p.exists():
            p.unlink()
    except OSError:
        pass

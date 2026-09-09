"""Legacy JSON layout -> SQLite one-time migration tests."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

from app import db


def _make_legacy(tmp_path: Path) -> Path:
    items = tmp_path / "items"
    projects = tmp_path / "projects"
    items.mkdir(parents=True, exist_ok=True)
    projects.mkdir(parents=True, exist_ok=True)
    wav = items / "m0001.wav"
    sf.write(str(wav), np.zeros(1600, dtype=np.float32), 16000)
    (items / "m0001.srt").write_text(
        "1\n00:00:00,000 --> 00:00:00,100\nhi\n", encoding="utf-8")
    (items / "m0001.preview.mp4").write_bytes(b"preview")
    proj = {"version": 1, "characters": [{"id": "c1", "name": "A", "color": "#000000"}],
            "segments": [], "speaker_segments": []}
    (projects / "m0002.json").write_text(json.dumps(proj), encoding="utf-8")
    # orphan wav without project
    sf.write(str(items / "m0003.wav"), np.zeros(1600, dtype=np.float32), 16000)
    return tmp_path


def test_migrate_legacy_layout(tmp_path: Path) -> None:
    _make_legacy(tmp_path)
    conn = db.get_conn(tmp_path)
    rows = db.fetch_all_items(conn)
    assert len(rows) == 2
    by_name = {r["name"]: r for r in rows}
    r1 = by_name["m0001"]
    assert r1["preview_mp4"] is not None and r1["preview_mp4"].endswith("m0001.preview.mp4")
    assert json.loads(r1["extra"])["subs_file"].endswith("m0001.srt")
    assert r1["id"].startswith("m-")
    assert r1["project_id"].startswith("p-")
    proj_raw = db.fetch_project(conn, r1["id"])
    assert proj_raw is not None
    # per-item blob no longer stores characters (project-level pool owns them)
    assert "characters" not in json.loads(proj_raw)
    pool = json.loads(db.fetch_project_record(conn, r1["project_id"])["extra"])
    assert pool["characters"][0]["name"] == "A"
    r3 = by_name["m0003"]
    assert db.fetch_project(conn, r3["id"]) is None


def test_migrate_only_runs_once(tmp_path: Path) -> None:
    _make_legacy(tmp_path)
    conn = db.get_conn(tmp_path)
    n0 = len(db.fetch_all_items(conn))
    assert n0 == 2
    # re-running migration on a non-empty DB must be a no-op
    db.migrate_legacy(tmp_path, conn)
    assert len(db.fetch_all_items(conn)) == n0


def test_no_legacy_no_rows(tmp_path: Path) -> None:
    conn = db.get_conn(tmp_path)
    assert db.fetch_all_items(conn) == []
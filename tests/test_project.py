"""project 持久化单元测试：角色池/片段/说话人分段 JSON 读写。"""
from __future__ import annotations

from pathlib import Path

from app import project as pr


def test_default_project() -> None:
    p = pr.default_project()
    assert p["version"] == 1
    assert p["characters"] == []
    assert p["segments"] == []
    assert p["speaker_segments"] == []


def test_save_load_roundtrip(tmp_path: Path) -> None:
    item_id = "m0001"
    data = {
        "version": 1,
        "characters": [{"id": "c1", "name": "A", "color": "#e5484d",
                        "speakerLabels": ["说话人1"], "created": 1}],
        "segments": [{"id": "s1", "start": 0.0, "end": 2.0,
                      "text": "こんにちは", "language": "JP",
                      "speakerLabel": "说话人1", "characterId": "c1"}],
        "speaker_segments": [{"start": 0.0, "end": 2.0, "label": "说话人1"}],
    }
    pr.save_project(tmp_path, item_id, data)
    got = pr.load_project(tmp_path, item_id)
    assert got["characters"][0]["name"] == "A"
    assert got["segments"][0]["characterId"] == "c1"
    assert got["speaker_segments"][0]["label"] == "说话人1"


def test_load_missing_returns_default(tmp_path: Path) -> None:
    p = pr.load_project(tmp_path, "m_none")
    assert p["characters"] == []
    assert p["segments"] == []


def test_load_corrupt_returns_default(tmp_path: Path) -> None:
    from app import db
    conn = db.get_conn(tmp_path)
    db.upsert_project(conn, "mX", "{ not json")
    p = pr.load_project(tmp_path, "mX")
    assert p["characters"] == []


def test_delete_project(tmp_path: Path) -> None:
    from app import db
    pr.save_project(tmp_path, "mX", pr.default_project())
    conn = db.get_conn(tmp_path)
    assert db.fetch_project(conn, "mX") is not None
    pr.delete_project(tmp_path, "mX")
    assert db.fetch_project(conn, "mX") is None


def test_next_color_distinct() -> None:
    colors = [pr.next_color() for _ in range(10)]
    assert len(set(colors)) == 10

def test_pool_roundtrip(tmp_path: Path) -> None:
    from app import db
    pid = db.ensure_default_project(db.get_conn(tmp_path))
    chars = [{"id": "c1", "name": "A", "color": "#e5484d",
              "speakerLabels": ["m1:\u8bf4\u8bdd\u4eba1"]}]
    pr.save_pool(tmp_path, pid, chars)
    got = pr.load_pool(tmp_path, pid)
    assert got["characters"][0]["name"] == "A"
    pr.save_pool(tmp_path, pid, [])
    assert pr.load_pool(tmp_path, pid)["characters"] == []


def test_load_pool_missing_returns_default(tmp_path: Path) -> None:
    assert pr.load_pool(tmp_path, "p-none")["characters"] == []

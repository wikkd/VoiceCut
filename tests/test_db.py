"""SQLite storage layer + MediaStore unit tests."""
from __future__ import annotations

import json
from pathlib import Path

from app import db
from app.media_store import MediaItem, MediaStore


def test_store_roundtrip_and_reload(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    item = MediaItem(
        id=store.new_id(), name="demo", wav_path=tmp_path / "a.wav",
        duration=3.0, sample_rate=48000, kind="audio",
        extra={"peaks": [[-0.1, 0.2]]},
    )
    store.add(item)
    got = store.get(item.id)
    assert got is not None and got.name == "demo"
    assert got.extra["peaks"] == [[-0.1, 0.2]]
    # a second store over the same workdir reloads persisted rows
    store2 = MediaStore(tmp_path)
    assert store2.get(item.id) is not None
    assert store2.all()[0].extra["peaks"] == [[-0.1, 0.2]]


def test_new_id_unique_and_stable(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    ids = {store.new_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("m-") for i in ids)


def test_remove_cascades_project(tmp_path: Path) -> None:
    from app import project as pr
    store = MediaStore(tmp_path)
    item = MediaItem(id=store.new_id(), name="x", wav_path=tmp_path / "x.wav",
                     duration=1.0, sample_rate=48000)
    store.add(item)
    pr.save_project(tmp_path, item.id, pr.default_project())
    conn = db.get_conn(tmp_path)
    assert db.fetch_project(conn, item.id) is not None
    store.remove(item.id)
    assert db.fetch_project(conn, item.id) is None


def test_persist_updates_extra(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    item = MediaItem(id=store.new_id(), name="x", wav_path=tmp_path / "x.wav",
                     duration=1.0, sample_rate=48000)
    store.add(item)
    item.extra["subs_file"] = "subs.srt"
    store.persist(item)
    conn = db.get_conn(tmp_path)
    row = db.fetch_item(conn, item.id)
    assert row is not None
    assert json.loads(row["extra"])["subs_file"] == "subs.srt"


def test_project_roundtrip_via_db(tmp_path: Path) -> None:
    from app import project as pr
    data = pr.default_project()
    data["segments"] = [{"id": "s1", "start": 0.0, "end": 1.0, "text": "hi"}]
    pr.save_project(tmp_path, "m-abc123", data)
    got = pr.load_project(tmp_path, "m-abc123")
    assert got["segments"][0]["text"] == "hi"
    assert pr.load_project(tmp_path, "m-none")["characters"] == []
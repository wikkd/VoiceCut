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

def test_projects_crud_and_default(tmp_path: Path) -> None:
    conn = db.get_conn(tmp_path)
    pid = db.ensure_default_project(conn)
    assert pid.startswith("p-")
    recs = db.fetch_project_records(conn)
    assert [r["id"] for r in recs] == [pid]
    db.insert_project(conn, "p-x", "proj", 1.0, 1.0, {"characters": []})
    db.rename_project_record(conn, "p-x", "renamed")
    assert db.fetch_project_record(conn, "p-x")["name"] == "renamed"
    db.delete_project_record(conn, "p-x")
    assert db.fetch_project_record(conn, "p-x") is None


def test_items_filter_by_project_and_assign(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    pid = db.ensure_default_project(db.get_conn(tmp_path))
    it1 = MediaItem(id=store.new_id(), name="a", wav_path=tmp_path / "a.wav",
                    duration=1.0, sample_rate=48000, project_id=pid)
    it2 = MediaItem(id=store.new_id(), name="b", wav_path=tmp_path / "b.wav",
                    duration=1.0, sample_rate=48000, project_id="p-other")
    store.add(it1)
    store.add(it2)
    assert [i.id for i in store.by_project(pid)] == [it1.id]
    assert [i.id for i in store.by_project("p-other")] == [it2.id]
    store.set_project(it2.id, pid)
    assert len(store.by_project(pid)) == 2
    assert db.fetch_item(db.get_conn(tmp_path), it2.id)["project_id"] == pid


def test_v1_to_v2_migration_pools_characters(tmp_path: Path) -> None:
    import sqlite3
    path = tmp_path / "voicecut.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE items (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'audio',
            wav_path TEXT NOT NULL, preview_mp4 TEXT, source TEXT, derived_from TEXT,
            duration REAL NOT NULL DEFAULT 0, sample_rate INTEGER NOT NULL DEFAULT 48000,
            created REAL NOT NULL, extra TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE projects (item_id TEXT PRIMARY KEY, data TEXT NOT NULL DEFAULT '{}');
    """)
    conn.execute("INSERT INTO items (id, name, kind, wav_path, created) VALUES ('m-1','a','audio','x.wav',1)")
    conn.execute("INSERT INTO projects (item_id, data) VALUES ('m-1', ?)", (
        json.dumps({"characters": [{"id": "c1", "name": "A",
                                    "speakerLabels": ["\u8bf4\u8bdd\u4eba1"]}],
                    "segments": [], "speaker_segments": []}),))
    conn.commit()
    conn.close()
    db.reset_conns()
    conn2 = db.get_conn(tmp_path)
    proj_raw = db.fetch_project(conn2, "m-1")
    assert proj_raw is not None
    assert "characters" not in json.loads(proj_raw)
    pid = db.ensure_default_project(conn2)
    pool = json.loads(db.fetch_project_record(conn2, pid)["extra"])
    assert pool["characters"][0]["speakerLabels"] == ["m-1:\u8bf4\u8bdd\u4eba1"]
    assert db.fetch_item(conn2, "m-1")["project_id"] == pid
    # idempotent: re-connect must not re-pool or drop
    db.reset_conns()
    conn3 = db.get_conn(tmp_path)
    pool2 = json.loads(db.fetch_project_record(conn3, pid)["extra"])
    assert len(pool2["characters"]) == 1

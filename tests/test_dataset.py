"""Dataset export: merged cut+trim chain + cancellation."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.dataset import DatasetSegment, export_dataset
from app.ffmpeg_util import media_duration
from app.tasks import TaskCancelled


def test_export_dataset_writes_files(sample_wav: Path, tmp_path: Path) -> None:
    segs = [
        DatasetSegment(start=0.2, end=1.5, text="hello", language="JP", speaker="s1"),
        DatasetSegment(start=1.6, end=2.6, text="world", language="JP", speaker="s1"),
        DatasetSegment(start=0.3, end=0.5, text="", language="JP", speaker="s1"),
    ]
    out = export_dataset(sample_wav, segs, tmp_path / "ds")
    assert out["count"] == 2
    assert out["skipped"] and out["skipped"][0]["seg"]["text"] == ""
    files = sorted((tmp_path / "ds").glob("*.wav"))
    assert len(files) == 2
    for w in files:
        assert media_duration(w) >= 0.8 - 0.01
    list_file = tmp_path / "ds" / "list.txt"
    assert list_file.exists()
    lines = list_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].split("|")[1:] == ["s1", "JP", "hello"]


def test_export_dataset_cancel(sample_wav: Path, tmp_path: Path) -> None:
    segs = [DatasetSegment(start=0.2, end=1.5, text="hello", language="JP", speaker="s1")]

    class _Tasks:
        def __init__(self, cancelled: bool) -> None:
            self._c = cancelled

        def cancelled(self, tid: str) -> bool:
            return self._c

        def update(self, *a, **k) -> None:
            pass

    with pytest.raises(TaskCancelled):
        export_dataset(sample_wav, segs, tmp_path / "ds2",
                       tasks=_Tasks(True), task_id="t1")
    out = export_dataset(sample_wav, segs, tmp_path / "ds3",
                         tasks=_Tasks(False), task_id="t1")
    assert out["count"] == 1

def test_export_dataset_multi_source(sample_wav: Path, tmp_path: Path) -> None:
    sources = {"i1": sample_wav, "i2": sample_wav}
    segs = [
        DatasetSegment(item_id="i1", start=0.2, end=1.5, text="hello", language="JP", speaker="s1"),
        DatasetSegment(item_id="i2", start=1.6, end=2.6, text="world", language="JP", speaker="s2"),
    ]
    out = export_dataset(sources, segs, tmp_path / "dsm")
    assert out["count"] == 2
    lines = (tmp_path / "dsm" / "list.txt").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].split("|")[1:3] == ["s1", "JP"]
    assert lines[1].split("|")[1:3] == ["s2", "JP"]
    assert sorted(f.name for f in (tmp_path / "dsm").glob("*.wav")) == ["001.wav", "002.wav"]


def test_export_dataset_multi_source_missing_item(tmp_path: Path) -> None:
    import pytest as _pytest
    segs = [DatasetSegment(item_id="i1", start=0.2, end=1.5, text="hello")]
    with _pytest.raises(KeyError):
        export_dataset({}, segs, tmp_path / "dsx")
    segs2 = [DatasetSegment(start=0.2, end=1.5, text="hello")]
    with _pytest.raises(ValueError):
        export_dataset({}, segs2, tmp_path / "dsy")

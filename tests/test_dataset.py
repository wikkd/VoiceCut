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
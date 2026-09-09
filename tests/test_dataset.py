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


def test_export_dataset_per_speaker_layout(sample_wav, tmp_path) -> None:
    from app.dataset import DatasetSegment, export_dataset

    segs = [
        DatasetSegment(start=0.2, end=1.5, text="one", language="JP", speaker="s1"),
        DatasetSegment(start=1.6, end=2.6, text="two", language="JP", speaker="s1"),
        DatasetSegment(start=0.2, end=1.5, text="three", language="JP", speaker="s2"),
        DatasetSegment(start=0.3, end=0.5, text="", language="JP", speaker="s2"),
    ]
    out = export_dataset(sample_wav, segs, tmp_path / "ds", layout="per_speaker", val_ratio=0.5)
    assert out["count"] == 3
    assert out["layout"] == "per_speaker"
    assert len(out["speakers"]) == 2
    # s1: 2 clips, N=round(1/0.5)=2 -> gi=1 is val -> train 1, val 1
    s1 = tmp_path / "ds" / "s1"
    assert sorted(p.name for p in (s1 / "train").glob("*.wav")) == ["001.wav"]
    assert sorted(p.name for p in (s1 / "val").glob("*.wav")) == ["001.wav"]
    lines = (s1 / "list.txt").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert all("|s1|JP|" in ln for ln in lines)
    val_lines = (s1 / "val_list.txt").read_text(encoding="utf-8").strip().splitlines()
    assert len(val_lines) == 1
    # s2: 1 valid clip -> train only, no val dir
    s2 = tmp_path / "ds" / "s2"
    assert len([p for p in (s2 / "train").glob("*.wav")]) == 1
    assert not (s2 / "val").exists()
    # files are out_dir-relative
    assert out["files"] == ["s1/train/001.wav", "s1/val/001.wav", "s2/train/001.wav"]


def test_export_dataset_per_speaker_unassigned_default(sample_wav, tmp_path) -> None:
    from app.dataset import DatasetSegment, export_dataset

    segs = [DatasetSegment(start=0.2, end=1.5, text="hello", language="JP", speaker="")]
    out = export_dataset(sample_wav, segs, tmp_path / "dsu", layout="per_speaker")
    d = tmp_path / "dsu" / "speaker"
    assert (d / "train" / "001.wav").exists()
    lines = (d / "list.txt").read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].split("|")[1] == "speaker"
    assert out["speakers"][0]["name"] == "speaker"


def test_export_dataset_flat_unchanged(sample_wav, tmp_path) -> None:
    from app.dataset import DatasetSegment, export_dataset

    segs = [DatasetSegment(start=0.2, end=1.5, text="hello", language="JP", speaker="s1")]
    out = export_dataset(sample_wav, segs, tmp_path / "dsf")
    assert out["layout"] == "flat"
    assert sorted(p.name for p in (tmp_path / "dsf").glob("*.wav")) == ["001.wav"]
    assert out["files"] == ["001.wav"]

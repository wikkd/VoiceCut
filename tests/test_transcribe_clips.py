"""transcribe_clips_full: single-pass word->clip mapping tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import transcribe
from app.tasks import TaskCancelled


class _FakeWord:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class _FakeSeg:
    def __init__(self, start: float, end: float, words: list) -> None:
        self.start = start
        self.end = end
        self.words = words


class _FakeInfo:
    duration = 10.0


class _FakeModel:
    def transcribe(self, path, **kw):
        assert kw.get("word_timestamps") is True
        segs = [
            _FakeSeg(0.0, 2.0, [
                _FakeWord(0.0, 0.5, "a"),
                _FakeWord(1.0, 1.5, "b"),
                _FakeWord(1.6, 1.9, "c"),
            ]),
            _FakeSeg(4.0, 6.0, [
                _FakeWord(4.1, 4.4, "d"),
                _FakeWord(5.2, 5.5, "e"),
            ]),
        ]
        return iter(segs), _FakeInfo()


def _patch(monkeypatch) -> None:
    monkeypatch.setattr(transcribe, "_load_model", lambda m, t: _FakeModel())
    monkeypatch.setattr(transcribe, "_init_runtime", lambda: None)


def test_clip_word_mapping(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"x")
    clips = [(0.0, 1.2), (1.2, 2.0), (3.0, 5.3), (8.0, 9.0)]
    texts = transcribe.transcribe_clips_full(wav, clips, language="ja", model="medium")
    # midpoints: a=0.25, b=1.25, c=1.75, d=4.25, e=5.35
    assert texts == ["a", "bc", "d", ""]


def test_single_pass_single_model_load(monkeypatch, tmp_path: Path) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(transcribe, "_load_model",
                        lambda m, t: (calls.__setitem__("n", calls["n"] + 1) or _FakeModel()))
    monkeypatch.setattr(transcribe, "_init_runtime", lambda: None)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"x")
    transcribe.transcribe_clips_full(wav, [(0, 2), (2, 4), (4, 6)], language="ja")
    assert calls["n"] == 1


def test_cancelled_raises(monkeypatch, tmp_path: Path) -> None:
    _patch(monkeypatch)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"x")
    with pytest.raises(TaskCancelled):
        transcribe.transcribe_clips_full(wav, [(0, 1)], cancelled_cb=lambda: True)
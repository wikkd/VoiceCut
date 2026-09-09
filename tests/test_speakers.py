"""speakers 单元测试：聚类逻辑 / 生成流程（monkeypatch 嵌入函数避免模型）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from app import speakers


def _two_tone_wav(path: Path, sr: int = 16000) -> None:
    n = sr
    t1 = (np.sin(2 * np.pi * 180 * np.arange(n) / sr) * 0.3).astype(np.float32)
    t2 = (np.sin(2 * np.pi * 420 * np.arange(n) / sr) * 0.3).astype(np.float32)
    sf.write(str(path), np.concatenate([t1, t2]), sr)


def test_cluster_two_groups() -> None:
    a = np.tile(np.array([1.0, 0, 0, 0, 0, 0, 0, 0]), (3, 1))
    b = np.tile(np.array([0.0, 1, 0, 0, 0, 0, 0, 0]), (3, 1))
    labels = speakers._cluster_labels(list(a) + list(b))
    assert len(set(labels)) == 2
    assert len(set(labels[:3])) == 1
    assert len(set(labels[3:])) == 1


def test_mid_window_whole_when_short() -> None:
    seg = np.zeros(8000)
    w = speakers._mid_window(seg, 16000)
    assert w.size == 8000  # < 1s -> whole window


def test_mid_window_slices_center() -> None:
    seg = np.zeros(16000 * 4)  # 4s
    seg[16000 * 2 - 8000:16000 * 2 + 8000] = 1.0  # mark middle 1s
    w = speakers._mid_window(seg, 16000)
    assert w.size == 16000
    assert np.all(w == 1.0)  # center window is the marked region


def test_generate_ecapa(monkeypatch, tmp_path: Path) -> None:
    wav = tmp_path / "t.wav"
    _two_tone_wav(wav)
    subs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]

    def fake_emb(mono, sr, start, end):
        return np.array([1.0, 0.0, 0.0]) if (start + end) / 2 < 1 else np.array([0.0, 1.0, 0.0])

    monkeypatch.setattr(speakers, "_ecapa_embedding", fake_emb)
    res = speakers.generate_speakers(str(wav), subs)
    assert res["quality"] == "ecapa"
    assert res["n_speakers"] == 2
    assert res["total"] == 2 and res["labeled"] == 2
    assert res["speaker_segments"][0]["label"] != res["speaker_segments"][1]["label"]


def test_generate_mfcc_fallback(monkeypatch, tmp_path: Path) -> None:
    wav = tmp_path / "t.wav"
    _two_tone_wav(wav)
    subs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]

    def boom(*_a, **_k):
        raise RuntimeError("no model")

    monkeypatch.setattr(speakers, "_ecapa_embedding", boom)
    res = speakers.generate_speakers(str(wav), subs)
    assert res["quality"] == "mfcc"
    assert res["total"] == 2


def test_generate_labels_ordered_by_first_appearance(monkeypatch, tmp_path: Path) -> None:
    wav = tmp_path / "t.wav"
    _two_tone_wav(wav)
    subs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]

    # B appears first -> 说话人1 for B, 说话人2 for A
    def fake_emb(mono, sr, start, end):
        return np.array([0.0, 1.0, 0.0]) if (start + end) / 2 < 1 else np.array([1.0, 0.0, 0.0])

    monkeypatch.setattr(speakers, "_ecapa_embedding", fake_emb)
    res = speakers.generate_speakers(str(wav), subs)
    assert res["speaker_segments"][0]["label"] == "说话人1"
    assert res["speaker_segments"][1]["label"] == "说话人2"

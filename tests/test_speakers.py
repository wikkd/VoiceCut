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

def test_label_embeddings_returned(monkeypatch, tmp_path: Path) -> None:
    wav = tmp_path / "t.wav"
    _two_tone_wav(wav)
    subs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]

    def fake_emb(mono, sr, start, end):
        return np.array([1.0, 0.0, 0.0]) if (start + end) / 2 < 1 else np.array([0.0, 1.0, 0.0])

    monkeypatch.setattr(speakers, "_ecapa_embedding", fake_emb)
    res = speakers.generate_speakers(str(wav), subs)
    assert set(res["label_embeddings"].keys()) == {"\u8bf4\u8bdd\u4eba1", "\u8bf4\u8bdd\u4eba2"}
    for v in res["label_embeddings"].values():
        assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6


def test_embedding_roundtrip() -> None:
    arr = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    b = speakers.embedding_to_b64(arr)
    out = speakers.embedding_from_b64(b)
    assert out is not None and np.allclose(out, arr)
    assert speakers.embedding_from_b64("bad!!") is None
    assert speakers.embedding_from_b64("") is None


def test_match_labels_merges_same_voice() -> None:
    base = np.array([1.0, 0.0, 0.0])
    chars = [{"id": "c1", "name": "A", "speakerLabels": ["i1:\u8bf4\u8bdd\u4eba1"],
              "embedding": speakers.embedding_to_b64(base), "emb_count": 1}]
    same = np.array([0.9, 0.1, 0.0])
    assignments, chars2, created = speakers.match_labels_to_pool(
        "i2", {"\u8bf4\u8bdd\u4eba1": same}, chars, threshold=0.82)
    assert assignments["\u8bf4\u8bdd\u4eba1"] == "c1"
    assert created == []
    assert "i2:\u8bf4\u8bdd\u4eba1" in chars2[0]["speakerLabels"]
    assert chars2[0]["emb_count"] == 2


def test_match_labels_creates_new_for_different_voice() -> None:
    base = np.array([1.0, 0.0, 0.0])
    chars = [{"id": "c1", "name": "A", "speakerLabels": [],
              "embedding": speakers.embedding_to_b64(base), "emb_count": 1}]
    other = np.array([0.0, 1.0, 0.0])
    assignments, chars2, created = speakers.match_labels_to_pool(
        "i2", {"\u8bf4\u8bdd\u4eba2": other}, chars, threshold=0.82)
    assert assignments["\u8bf4\u8bdd\u4eba2"] == created[0]
    assert created and len(chars2) == 2
    assert "i2:\u8bf4\u8bdd\u4eba2" in chars2[1]["speakerLabels"]


def test_match_labels_threshold_boundary() -> None:
    base = np.array([1.0, 0.0, 0.0])
    low = np.array([0.8, 0.6, 0.0])  # cosine 0.8 < 0.82 -> new character
    chars = [{"id": "c1", "name": "A", "speakerLabels": [],
              "embedding": speakers.embedding_to_b64(base), "emb_count": 1}]
    assignments, chars2, created = speakers.match_labels_to_pool(
        "i", {"x": low}, chars, threshold=0.82)
    assert assignments["x"] == created[0]
    assert len(chars2) == 2


def test_match_labels_reuses_existing_label() -> None:
    base = np.array([1.0, 0.0, 0.0])
    chars = [{"id": "c1", "name": "A", "speakerLabels": ["i1:\u8bf4\u8bdd\u4eba1"],
              "embedding": speakers.embedding_to_b64(base), "emb_count": 1}]
    assignments, chars2, created = speakers.match_labels_to_pool(
        "i1", {"\u8bf4\u8bdd\u4eba1": base}, chars, threshold=0.82)
    assert assignments["\u8bf4\u8bdd\u4eba1"] == "c1"
    assert created == []


def test_window_ranges_coverage() -> None:
    assert list(speakers._window_ranges(0, 1.0)) == [(0.0, 0.8), (0.4, 1.0)]
    rng = list(speakers._window_ranges(0, 2.0))
    assert rng[0] == (0.0, 0.8) and rng[-1][1] == 2.0
    assert all(rng[i + 1][0] < rng[i][1] for i in range(len(rng) - 1))  # overlapping
    assert list(speakers._window_ranges(0, 0.2)) == []  # too short


def test_dominant_label_basic() -> None:
    segs = [{"start": 0, "end": 1, "label": "A"}, {"start": 1, "end": 2, "label": "B"}]
    assert speakers.dominant_label(0, 1, segs) == ("A", False)
    assert speakers.dominant_label(0, 2, segs) == ("A", True)   # 50/50 -> mixed
    assert speakers.dominant_label(0.5, 1.5, segs) == ("A", True)
    assert speakers.dominant_label(0, 2, segs, min_cover=0.6) == ("A", False)
    assert speakers.dominant_label(10, 12, segs) == (None, False)
    assert speakers.dominant_label(0, 2, []) == (None, False)


def test_dominant_label_tiny_overlap_not_mixed() -> None:
    segs = [{"start": 0, "end": 2, "label": "A"}, {"start": 1.8, "end": 2.0, "label": "B"}]
    # [0,2]: B only 0.2s of 2.2s labeled -> not mixed, dominant A
    assert speakers.dominant_label(0, 2, segs) == ("A", False)
    # [1.7,2]: A dominant (0.3s) with B only 0.2s -> not mixed
    assert speakers.dominant_label(1.7, 2.0, segs) == ("A", False)


def test_generate_splits_mixed_subtitle(monkeypatch, tmp_path: Path) -> None:
    """Whisper merges two speakers into ONE subtitle -> windowing splits them."""
    wav = tmp_path / "mix.wav"
    sr = 16000
    n = sr * 2
    t = np.arange(n) / sr
    sig = np.where(t < 1.0, np.sin(2 * np.pi * 180 * t), np.sin(2 * np.pi * 420 * t))
    sf.write(str(wav), sig.astype(np.float32), sr)
    subs = [{"start": 0.0, "end": 2.0}]

    def fake_emb(mono, sr, start, end):
        return np.array([1.0, 0.0, 0.0]) if (start + end) / 2 < 1.0 else np.array([0.0, 1.0, 0.0])

    monkeypatch.setattr(speakers, "_ecapa_embedding", fake_emb)
    res = speakers.generate_speakers(str(wav), subs)
    assert res["n_speakers"] == 2
    assert len(res["speaker_segments"]) == 2
    assert res["speaker_segments"][0]["label"] != res["speaker_segments"][1]["label"]
    assert res["sub_labels"][0]["mixed"] is True
    assert len(res["label_embeddings"]) == 2
    assert res["total"] == 1 and res["labeled"] == 1 and res["mixed"] == 1


def test_bind_segments_mixed_not_auto_bound() -> None:
    segs = [
        {"id": "s1", "start": 0, "end": 1.0, "text": "A", "speakerLabel": None, "characterId": None},
        {"id": "s2", "start": 0, "end": 2.0, "text": "AB", "speakerLabel": None, "characterId": None},
    ]
    spk = [
        {"start": 0, "end": 1.1, "label": "说话人1"},
        {"start": 0.9, "end": 2.0, "label": "说话人2"},
    ]
    char_of_label = {"说话人1": "c1", "说话人2": "c2"}
    out, mixed_count = speakers.bind_segments(segs, spk, char_of_label)
    # s1: mostly 说话人1, tiny 说话人2 overlap -> bound to c1, not mixed
    assert out[0]["speakerLabel"] == "说话人1" and out[0]["characterId"] == "c1"
    assert out[0]["mixed"] is False
    # s2: 50/50 two speakers -> mixed, no auto character
    assert out[1]["speakerLabel"] == "说话人1" and out[1]["mixed"] is True
    assert out[1]["characterId"] is None
    assert mixed_count == 1


def test_bind_segments_keeps_manual_character() -> None:
    segs = [{"id": "s1", "start": 0, "end": 1.0, "speakerLabel": "x", "characterId": "manual"}]
    spk = [{"start": 0, "end": 1.0, "label": "说话人1"}]
    out, _mixed = speakers.bind_segments(segs, spk, {"说话人1": "c1"})
    assert out[0]["speakerLabel"] == "说话人1"
    assert out[0]["characterId"] == "manual"  # manual assignment preserved

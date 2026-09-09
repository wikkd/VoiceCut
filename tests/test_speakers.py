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
    assert list(speakers._window_ranges(0, 1.0)) == [(0.0, 1.0), (0.5, 1.0)]
    rng = list(speakers._window_ranges(0, 2.0))
    assert rng[0] == (0.0, 1.4) and rng[-1][1] == 2.0
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
    """A whisper subtitle that merges two speakers is flagged mixed, not merged
    into one character; surrounding single-speaker subtitles map to 2 roles."""
    wav = tmp_path / "mix.wav"
    sr = 16000
    n = sr * 12
    t = np.arange(n) / sr
    sig = np.sin(2 * np.pi * 180 * t)  # placeholder tone (fake embeddings are patched)
    sf.write(str(wav), sig.astype(np.float32), sr)
    subs = [
        {"start": 0.0, "end": 3.0},    # speaker A only
        {"start": 4.0, "end": 8.0},    # A (4-6.5s) then B (6.5-8s) -> mixed
        {"start": 9.0, "end": 12.0},   # speaker B only
    ]

    def fake_emb(mono, sr, start, end):
        mid = (start + end) / 2
        if mid < 6.5:
            return np.array([1.0, 0.0, 0.0])  # A
        return np.array([0.0, 1.0, 0.0])      # B

    monkeypatch.setattr(speakers, "_ecapa_embedding", fake_emb)
    res = speakers.generate_speakers(str(wav), subs)
    assert res["n_speakers"] == 2
    assert len(res["label_embeddings"]) == 2
    assert res["sub_labels"][0]["mixed"] is False
    assert res["sub_labels"][1]["mixed"] is True   # merged subtitle flagged
    assert res["sub_labels"][2]["mixed"] is False
    assert res["mixed"] == 1
    assert res["total"] == 3 and res["labeled"] == 3
    # the mixed subtitle emits a second-speaker run so it is not silently merged
    labels = {s["label"] for s in res["speaker_segments"]}
    assert len(labels) == 2


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


def test_stale_characters_removes_only_item_garbage() -> None:
    chars = [
        {"id": "c1", "name": "说话人1", "speakerLabels": ["i1:说话人1"], "emb_count": 1},
        {"id": "c2", "name": "说话人2", "speakerLabels": ["i1:说话人2", "i2:说话人2"], "emb_count": 2},  # merged -> keep
        {"id": "c3", "name": "说话人3", "speakerLabels": ["i1:说话人3"], "emb_count": 1, "exp": "exp3"},  # trained -> keep
        {"id": "c4", "name": "マドカ", "speakerLabels": ["i1:说话人4"], "emb_count": 1},  # renamed -> keep
        {"id": "c5", "name": "说话人5", "speakerLabels": ["i2:说话人5"], "emb_count": 1},  # other item -> keep
    ]
    stale = speakers.stale_characters("i1", chars)
    assert [c["id"] for c in stale] == ["c1"]
    assert len(chars) == 5  # helper is pure: original list untouched


def test_stale_characters_no_embedding_chars_kept() -> None:
    # MFCC-era characters carry no embedding / emb_count -> still cleaned if
    # auto-named and single-item (they are the legacy garbage we want gone)
    chars = [{"id": "c1", "name": "说话人1", "speakerLabels": ["i1:说话人1"]}]
    assert [c["id"] for c in speakers.stale_characters("i1", chars)] == ["c1"]
    # real ECAPA-era garbage (embedding + emb_count=1, single-item) is also stale
    chars2 = [{"id": "c2", "name": "说话人2", "speakerLabels": ["i1:说话人2"],
               "embedding": "AAAA", "emb_count": 1}]
    assert [c["id"] for c in speakers.stale_characters("i1", chars2)] == ["c2"]


def test_cluster_adaptive_merges_noise() -> None:
    """Noisy per-window-like embeddings must NOT collapse to one-per-vector."""
    rng = np.random.default_rng(0)
    # 4 speaker centres + gaussian noise around each
    centres = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    X = []
    for c in centres:
        for _ in range(25):
            v = c + rng.normal(0, 0.15, 4)
            X.append(v / (np.linalg.norm(v) + 1e-9))
    labels = speakers._cluster_labels(X)
    k = len(set(labels))
    assert 2 <= k <= 8, f"expected a sane cluster count, got {k}"


# ── 项目级联合聚类：同一说话人跨素材保持同一标签 ──

def _fake_emb_abc(mono, sr, start, end):
    mid = (start + end) / 2
    if mid < 1.0:
        return np.array([1.0, 0.0, 0.0])   # A
    if mid < 2.0:
        return np.array([0.0, 1.0, 0.0])   # B
    return np.array([0.0, 0.0, 1.0])       # C


def _write_tone_wav(path: Path) -> None:
    sr = 16000
    n = sr * 4
    t = np.arange(n) / sr
    sf.write(str(path), (np.sin(2 * np.pi * 220 * t) * 0.3).astype(np.float32), sr)


def test_generate_project_shared_speaker_across_sources(monkeypatch, tmp_path: Path) -> None:
    """同一角色在素材 1 和素材 2 都出现时，项目级聚类应给同一说话人标签。"""
    w1 = tmp_path / "a.wav"; w2 = tmp_path / "b.wav"
    _write_tone_wav(w1); _write_tone_wav(w2)
    monkeypatch.setattr(speakers, "_ecapa_embedding", _fake_emb_abc)
    sources = [
        {"wav_path": str(w1), "subs": [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]},   # A, B
        {"wav_path": str(w2), "subs": [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]},   # A, C
    ]
    res = speakers.generate_speakers_project(sources)
    assert res["quality"] == "ecapa"
    assert res["n_speakers"] == 3
    assert len(res["label_embeddings"]) == 3
    assert len(res["items"]) == 2
    # A 在两个素材里都是「说话人1」，不会拆成两个角色
    assert res["items"][0]["sub_labels"][0]["label"] == "说话人1"
    assert res["items"][1]["sub_labels"][0]["label"] == "说话人1"
    assert res["items"][0]["sub_labels"][1]["label"] == "说话人2"   # B
    assert res["items"][1]["sub_labels"][1]["label"] == "说话人3"   # C
    # 说话人1 的片段同时出现在两个素材
    s0 = [s for s in res["items"][0]["speaker_segments"] if s["label"] == "说话人1"]
    s1 = [s for s in res["items"][1]["speaker_segments"] if s["label"] == "说话人1"]
    assert s0 and s1
    assert all(x["mixed"] is False for it in res["items"] for x in it["sub_labels"])


def test_generate_project_single_source_matches_wrapper(monkeypatch, tmp_path: Path) -> None:
    """单素材的项目级分析与原 generate_speakers 行为一致。"""
    w = tmp_path / "one.wav"
    _write_tone_wav(w)
    monkeypatch.setattr(speakers, "_ecapa_embedding", _fake_emb_abc)
    subs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]
    res = speakers.generate_speakers_project([{"wav_path": str(w), "subs": subs}])
    it = res["items"][0]
    assert it["total"] == 2 and it["labeled"] == 2
    assert len(it["speaker_segments"]) == 2
    assert res["n_speakers"] == 2


def test_generate_project_progress_and_skip_empty(monkeypatch, tmp_path: Path) -> None:
    """进度回调被调用，且完全没有窗口的素材不会崩。"""
    w1 = tmp_path / "a.wav"; w2 = tmp_path / "b.wav"
    _write_tone_wav(w1); _write_tone_wav(w2)
    monkeypatch.setattr(speakers, "_ecapa_embedding", _fake_emb_abc)
    calls = []
    sources = [
        {"wav_path": str(w1), "subs": [{"start": 0.0, "end": 1.0}]},
        {"wav_path": str(w2), "subs": []},
    ]
    res = speakers.generate_speakers_project(sources, progress_cb=calls.append)
    assert len(calls) >= 2
    assert res["items"][1]["total"] == 0 and res["items"][1]["labeled"] == 0
    assert res["n_speakers"] == 1

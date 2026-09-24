"""speakers 单元测试：聚类逻辑 / 生成流程（monkeypatch 嵌入函数避免模型）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
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


def test_read_mono16k_decodes_via_ffmpeg(tmp_path: Path) -> None:
    """read_mono16k 经 ffmpeg 输出 16k 单声道，时长/幅度与源一致。"""
    sr = 48000
    n = sr * 2
    tone = (np.sin(2 * np.pi * 440 * np.arange(n) / sr) * 0.125).astype(np.float32)
    wav = tmp_path / "src48k.wav"
    sf.write(str(wav), tone, sr)

    mono, out_sr = speakers.read_mono16k(wav)
    assert out_sr == 16000
    assert mono.dtype == np.float32
    assert mono.ndim == 1
    assert mono.shape[0] == pytest.approx(2 * 16000, abs=1600)
    assert np.isfinite(mono).all()
    assert float(np.max(np.abs(mono))) > 0.05  # 440Hz 正弦被保留


# ── 新算法：稳健嵌入聚合 / 分离度最优聚类 / 相似簇合并 / 记忆强绑定 ──────────

def test_aggregate_subtitle_embedding_trims_outlier_window() -> None:
    """被一个正交噪声窗口污染的字幕，聚合嵌入仍贴近主体方向（而非被平均拉偏）。"""
    main = np.array([1.0, 0.0, 0.0, 0.0])
    noise = np.array([0.0, 1.0, 0.0, 0.0])
    vecs = [(0.0, 0.5, main), (0.5, 1.0, main), (1.0, 1.5, main), (1.5, 2.0, noise)]
    agg = speakers._aggregate_subtitle_embedding(vecs)
    assert agg is not None
    assert speakers._cos(agg, main) > 0.95
    # 单一窗口时保持原向量
    assert speakers._cos(speakers._aggregate_subtitle_embedding([(0.0, 1.0, main)]), main) > 0.999


def test_cluster_separates_similar_but_distinct_speakers() -> None:
    """两个余弦 0.8 的相似说话人应分开，不会被固定阈值误并成一个角色。"""
    rng = np.random.default_rng(0)
    c1 = np.array([1.0, 0.0, 0.0, 0.0])
    c2 = np.array([0.8, 0.6, 0.0, 0.0])
    c2 = c2 / np.linalg.norm(c2)
    X: list = []
    for c in (c1, c2):
        for _ in range(6):
            v = c + rng.normal(0, 0.04, 4)
            X.append(v / np.linalg.norm(v))
    labels = speakers._cluster_labels(X, min_k=2, max_k=15)
    assert len(set(labels)) == 2
    assert len(set(labels[:6])) == 1
    assert len(set(labels[6:])) == 1


def test_merge_similar_clusters_merges_split_voice() -> None:
    """同一声线因波动被拆成两个相似簇（质心余弦 >= 0.90）时自动补并。"""
    X = np.stack([
        np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0]),
        np.array([0.92, 0.39, 0.0]), np.array([0.92, 0.39, 0.0]),
        np.array([0.0, 1.0, 0.0]), np.array([0.0, 1.0, 0.0]),
    ])
    labels = [0, 0, 0, 1, 1, 2, 2]
    out = speakers._merge_similar_clusters(X, labels, merge_thr=0.90)
    assert len(set(out)) == 2
    assert out[0] == out[3]   # 簇 0 与簇 1 合并
    assert out[5] != out[0]   # 簇 2 保持独立


def test_match_labels_strong_reuses_existing_character() -> None:
    """记忆强绑定：高相似度（且唯一最优）的标签直接并入已有角色，不新建。"""
    chars = [{
        "id": "char_1", "name": "A", "speakerLabels": [],
        "embedding": speakers.embedding_to_b64(np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        "emb_count": 1,
    }]
    label_emb = {"说话人1": np.array([0.95, 0.31, 0.0], dtype=np.float32)}  # 与 [1,0,0] 余弦 ≈0.95
    assign, chars_out, matched = speakers.match_labels_strong("p1", label_emb, chars)
    assert assign == {"说话人1": "char_1"}
    assert matched == ["说话人1"]
    assert len(chars_out) == 1
    assert chars_out[0]["emb_count"] == 2  # 代表声纹被运行平均更新


def test_project_rerun_reuses_character_via_memory() -> None:
    """跨次识别：第一次新建角色，第二次同一声线（带轻微变化）经强绑定自动归并。"""
    emb1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    _assign1, chars, _created = speakers.match_labels_to_pool("p1", {"说话人1": emb1}, [])
    assert len(chars) == 1
    emb2 = np.array([0.98, 0.2, 0.0], dtype=np.float32)  # 同人、略有变化
    strong, chars2, matched = speakers.match_labels_strong("p1", {"说话人1": emb2}, chars)
    assert strong == {"说话人1": chars[0]["id"]}
    assert matched == ["说话人1"]
    assert len(chars2) == 1
    assert chars2[0]["emb_count"] == 2


def test_match_labels_strong_requires_clear_winner() -> None:
    """两个候选角色都同样接近时（无显著胜者），不强制归并，留给人工/弱匹配。"""
    chars = [
        {"id": "char_a", "name": "A", "speakerLabels": [],
         "embedding": speakers.embedding_to_b64(np.array([0.9, 0.1, 0.0], dtype=np.float32)), "emb_count": 1},
        {"id": "char_b", "name": "B", "speakerLabels": [],
         "embedding": speakers.embedding_to_b64(np.array([0.9, -0.1, 0.0], dtype=np.float32)), "emb_count": 1},
    ]
    label_emb = {"说话人1": np.array([1.0, 0.0, 0.0], dtype=np.float32)}  # 与两者余弦几乎相同
    assign, _chars_out, matched = speakers.match_labels_strong("p1", label_emb, chars)
    assert assign == {} and matched == []


def test_match_labels_strong_low_similarity_not_bound() -> None:
    """相似度过低（< 0.80）不自动归并。"""
    chars = [{
        "id": "char_a", "name": "A", "speakerLabels": [],
        "embedding": speakers.embedding_to_b64(np.array([1.0, 0.0, 0.0], dtype=np.float32)), "emb_count": 1,
    }]
    label_emb = {"说话人1": np.array([0.5, 0.87, 0.0], dtype=np.float32)}  # 余弦 ≈0.5
    assign, _chars_out, matched = speakers.match_labels_strong("p1", label_emb, chars)
    assert assign == {} and matched == []


def test_merge_similar_clusters_default_is_conservative() -> None:
    """默认合并阈值(0.95)不应把「相似但不同」的两个声音(余弦 0.92)并成一个。

    实测真实 ECAPA 的同类内一致性低至 ~0.93、异类可达 ~0.90，因此合并阈值
    必须高于异类相似度，只用来修补「同一人因噪声被拆簇」的过分割。
    """
    sim = 0.92  # 相似但不同
    X = np.stack([
        np.array([1.0, 0.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([sim, float(np.sqrt(1 - sim * sim)), 0.0]),
        np.array([sim, float(np.sqrt(1 - sim * sim)), 0.0]),
    ])
    labels = [0, 0, 1, 1]
    out = speakers._merge_similar_clusters(X, labels)   # 使用默认阈值
    assert len(set(out)) == 2  # 不误并


def test_cluster_handles_compressed_similarity_regime() -> None:
    """真实 ECAPA 相似度整体偏高（同类 0.95+、异类 0.85~0.92）时，
    聚类仍应按「相对分离度」而不是绝对阈值把结构分出来。"""
    rng = np.random.default_rng(3)
    d = 8
    # 三个说话人方向：两两余弦约 0.88 / 0.85 / 0.9（压缩区间）
    c1 = np.zeros(d); c1[0] = 1.0
    c2 = np.zeros(d); c2[0] = 0.88; c2[1] = float(np.sqrt(1 - 0.88**2))
    c3 = np.zeros(d); c3[0] = 0.85; c3[1] = -0.20; c3[2] = float(np.sqrt(max(0.0, 1 - 0.85**2 - 0.04)))
    X: list = []
    for c in (c1, c2, c3):
        for _ in range(5):
            v = c + rng.normal(0, 0.015, d)   # 同类内一致性 ~0.97
            X.append(v / np.linalg.norm(v))
    labels = speakers._cluster_labels(X, min_k=2, max_k=15)
    # 至少要把最疏远的那一组分出来，不能全部塌成 1 个
    assert len(set(labels)) >= 2
    # 同类样本应聚在一起
    assert len(set(labels[:5])) == 1
    assert len(set(labels[10:])) == 1


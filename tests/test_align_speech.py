"""对齐发音测试：链式字幕时间窗口收缩到实际语音区间（locked 跳过、绝不外扩）。"""
from __future__ import annotations

from app.autosplit import align_segments_to_speech, speech_ranges

# 语音区间：[1,4] [6,9] [12,15]；其余为静音（时长 20s）
SPEECH = [(1.0, 4.0), (6.0, 9.0), (12.0, 15.0)]
SILENCES = [(0.0, 1.0), (4.0, 6.0), (9.0, 12.0), (15.0, 20.0)]
DUR = 20.0


def test_speech_ranges_inverse() -> None:
    assert speech_ranges(DUR, SILENCES) == SPEECH
    assert speech_ranges(DUR, []) == [(0.0, 20.0)]        # 无静音 → 全程语音
    assert speech_ranges(0, SILENCES) == []


def test_align_shrinks_chained_windows() -> None:
    # 链式字幕：start == 上一段 end，窗口远大于语音
    segs = [
        {"id": "s1", "start": 0.0, "end": 5.0, "text": "a"},   # 语音 [1,4]
        {"id": "s2", "start": 5.0, "end": 11.0, "text": "b"},  # 语音 [6,9]
        {"id": "s3", "start": 11.0, "end": 20.0, "text": "c"}, # 语音 [12,15]
    ]
    changed = align_segments_to_speech(segs, DUR, SPEECH)
    assert changed == 3
    s1, s2, s3 = segs
    assert s1["start"] == 0.92 and s1["end"] == 4.08    # pad=0.08
    assert s2["start"] == 5.92 and s2["end"] == 9.08    # 5.92 = max(5, 6-0.08)
    assert s3["end"] == 15.08                           # min(20, 15+0.08)
    # 不再首尾相接
    assert s2["start"] - s1["end"] > 0.5
    assert s3["start"] - s2["end"] > 0.5


def test_align_never_expands_and_skips_locked() -> None:
    segs = [
        # 已精准在语音上：不外扩（pad 不突破原边界）
        {"id": "s1", "start": 1.0, "end": 4.0, "text": "exact"},
        # 锁定片段：人工成果不动
        {"id": "s2", "start": 5.0, "end": 11.0, "text": "手改", "locked": True},
    ]
    changed = align_segments_to_speech(segs, DUR, SPEECH)
    assert changed == 0
    assert segs[0]["start"] == 1.0 and segs[0]["end"] == 4.0
    assert segs[1]["start"] == 5.0 and segs[1]["end"] == 11.0


def test_align_keeps_window_without_speech() -> None:
    # 窗口内没有语音（整段落在静音里）→ 保持原样
    segs = [{"id": "s1", "start": 16.0, "end": 18.0, "text": "bgm?"}]
    assert align_segments_to_speech(segs, DUR, SPEECH) == 0
    assert segs[0]["start"] == 16.0 and segs[0]["end"] == 18.0


def test_align_discards_result_if_too_short() -> None:
    # 窗口几乎完全在语音内部，收缩量不足 min_keep → 保持原样
    segs = [{"id": "s1", "start": 13.9, "end": 14.6, "text": "mid"}]
    assert align_segments_to_speech(segs, DUR, SPEECH) == 0
    assert segs[0]["start"] == 13.9 and segs[0]["end"] == 14.6


def test_align_spanning_window_takes_first_to_last() -> None:
    # 一个窗口横跨多个语音区间（中间短静音被保留在窗口内）
    segs = [{"id": "s1", "start": 0.0, "end": 10.0, "text": "跨两段"}]
    assert align_segments_to_speech(segs, DUR, SPEECH) == 1
    assert segs[0]["start"] == 0.92 and segs[0]["end"] == 9.08


# ── 能量自适应静音检测（Silero 兜底）：带持续底噪也能找到句间空隙 ──

def test_adaptive_silence_with_noise_floor(tmp_path) -> None:
    import numpy as np
    import soundfile as sf

    from app.audio_ops import detect_silence_adaptive

    sr = 16000
    rng = np.random.default_rng(7)
    # 音频结构：噪底[0,1] 语音[1,4] 噪底[4,6] 语音[6,9] 噪底[9,10]
    # "语音"= 440Hz 正弦；"噪底"= 白噪声 -38dB（固定阈值 -35dB 探不到）
    parts = []
    for kind, sec in [("noise", 1.0), ("tone", 3.0), ("noise", 2.0),
                      ("tone", 3.0), ("noise", 1.0)]:
        n = int(sec * sr)
        if kind == "tone":
            a = 0.25 * np.sin(2 * np.pi * 440.0 * np.arange(n) / sr)
        else:
            a = rng.normal(0, 10 ** (-38.0 / 20.0), n)
        parts.append(a.astype(np.float32))
    wav = tmp_path / "adapt.wav"
    sf.write(wav, np.concatenate(parts), sr)

    sil = detect_silence_adaptive(wav, min_silence=0.3)
    # 应恰好找到 3 段静音（允许帧粒度误差 ±0.15s）
    assert len(sil) == 3, f"got {sil}"
    assert abs(sil[0][1] - 1.0) < 0.15    # [0, 1]
    assert abs(sil[1][0] - 4.0) < 0.15 and abs(sil[1][1] - 6.0) < 0.15
    assert abs(sil[2][0] - 9.0) < 0.15    # [9, 10]

    # 与对齐联动：链式窗口被正确收缩
    segs = [{"id": "s1", "start": 0.0, "end": 4.5, "text": "x"},   # 语音 [1,4]
            {"id": "s2", "start": 4.5, "end": 10.0, "text": "y"}]  # 语音 [6,9]
    assert align_segments_to_speech(segs, 10.0, speech_ranges(10.0, sil)) == 2
    eps = 0.06   # 帧粒度 20ms + pad 的容差
    assert abs(segs[0]["start"] - 0.92) < eps and abs(segs[0]["end"] - 4.08) < eps
    assert abs(segs[1]["start"] - 5.92) < eps and abs(segs[1]["end"] - 9.08) < eps
    assert segs[1]["start"] - segs[0]["end"] > 1.0   # 链式解除


def test_adaptive_low_dynamic_range_returns_empty(tmp_path) -> None:
    import numpy as np
    import soundfile as sf

    from app.audio_ops import detect_silence_adaptive

    sr = 16000
    rng = np.random.default_rng(3)
    # 全程恒定电平白噪声：无动态对比 → 返回 []（调用方保持原边界）
    wav = tmp_path / "flat.wav"
    sf.write(wav, rng.normal(0, 0.05, sr * 3).astype(np.float32), sr)
    assert detect_silence_adaptive(wav) == []


# ── detect_speech_ranges：Silero 封装（monkeypatch 神经推理，测接线） ──

def test_detect_speech_ranges_wraps_silero(tmp_path, monkeypatch) -> None:
    import faster_whisper.vad as vad_mod
    import numpy as np
    import soundfile as sf

    import app.audio_ops as ao

    captured = {}

    def fake_vad(audio, opts, sampling_rate=16000, **kw):
        captured["sr"] = sampling_rate
        captured["min_gap_ms"] = opts.min_silence_duration_ms
        return [{"start": 1600, "end": 64000},    # 0.1s ~ 4.0s
                {"start": 96000, "end": 144000}]  # 6.0s ~ 9.0s

    monkeypatch.setattr(vad_mod, "get_speech_timestamps", fake_vad)

    sr = 16000
    wav = tmp_path / "in.wav"
    sf.write(wav, np.zeros(sr * 10, dtype=np.float32), sr)
    out = ao.detect_speech_ranges(wav, min_gap=0.3)
    assert out == [(0.1, 4.0), (6.0, 9.0)]
    assert captured["sr"] == 16000 and captured["min_gap_ms"] == 300

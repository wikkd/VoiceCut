"""audio_ops 单元测试：波形峰值 / 质量指标 / 响度标准化 / 数据集校验。"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.audio_ops import (
    audio_metrics,
    compute_peaks,
    normalize_loudness,
    read_wav,
    validate_dataset_clip,
)


def test_read_wav(sample_wav: Path) -> None:
    data, sr = read_wav(sample_wav)
    assert sr == 48000
    assert data.shape[0] == pytest.approx(3 * 48000, abs=4800)
    assert np.isfinite(data).all()


def test_compute_peaks(sample_wav: Path) -> None:
    peaks = compute_peaks(sample_wav, max_points=1000)
    assert 1 <= len(peaks) <= 1000
    for lo, hi in peaks:
        assert lo <= hi
    # 440Hz 满幅正弦波：全局峰值应接近 ±1
    global_min = min(p[0] for p in peaks)
    global_max = max(p[1] for p in peaks)
    assert global_min < -0.1
    assert global_max > 0.1


def test_audio_metrics(sample_wav: Path) -> None:
    m = audio_metrics(sample_wav)
    assert m["duration"] == pytest.approx(3.0, abs=0.1)
    assert m["sample_rate"] == 48000
    assert -23.0 < m["rms_db"] < -19.0  # 0.125 振幅正弦 RMS ≈ -21.1dBFS
    assert -20.0 < m["peak_db"] < -17.0  # 0.125 振幅正弦峰值 ≈ -18.1dBFS
    assert m["clipping"] is False
    assert m["silence_ratio"] < 0.1


def test_normalize_loudness(sample_wav: Path, tmp_path: Path) -> None:
    out = normalize_loudness(sample_wav, tmp_path / "norm.wav", target_db=-20.0)
    m = audio_metrics(out)
    assert m["rms_db"] == pytest.approx(-20.0, abs=1.5)


def test_validate_dataset_clip(sample_wav: Path) -> None:
    from app.ffmpeg_util import export_segment

    r = validate_dataset_clip(sample_wav)          # 3s → 合规
    assert r["ok"] is True
    assert r["issues"] == []

    # 过短片段 → 不合规
    short = sample_wav.parent / "short.wav"
    export_segment(sample_wav, short, 0.0, 0.5, sample_rate=48000)
    r2 = validate_dataset_clip(short, min_dur=1.0)
    assert r2["ok"] is False
    assert any("过短" in i for i in r2["issues"])


def test_compute_peaks_streaming_matches_full_read(tmp_path: Path, sample_wav: Path) -> None:
    """流式分块峰值与整段读取的旧语义完全一致（长素材内存安全）。"""
    from app.audio_ops import read_wav
    from app.ffmpeg_util import export_segment

    long_wav = tmp_path / "long.wav"
    export_segment(sample_wav, long_wav, 0.0, 10.0, sample_rate=48000)
    data, _ = read_wav(long_wav)
    n = data.shape[0]
    max_points = 1000
    block = math.ceil(n / max_points)
    expected = []
    for i in range(0, n, block):
        seg = data[i : i + block]
        if seg.size:
            expected.append([float(seg.min()), float(seg.max())])

    got = compute_peaks(long_wav, max_points=max_points)
    assert len(got) == len(expected)
    for a, b in zip(got, expected):
        assert a[0] == pytest.approx(b[0], abs=1e-6)
        assert a[1] == pytest.approx(b[1], abs=1e-6)


def test_compute_peaks_stereo_downmix(tmp_path: Path) -> None:
    """多声道输入流式下混后峰值在合理区间。"""
    sr = 48000
    t = np.linspace(0, 1, sr)
    stereo = np.stack(
        [np.sin(2 * np.pi * 440 * t) * 0.5, np.sin(2 * np.pi * 440 * t) * 0.2],
        axis=1).astype(np.float32)
    p = tmp_path / "stereo.wav"
    sf.write(str(p), stereo, sr)
    peaks = compute_peaks(p, max_points=100)
    assert 1 <= len(peaks) <= 100
    hi = max(pk[1] for pk in peaks)
    assert 0.2 < hi <= 0.5


"""audio_ops 单元测试：波形峰值 / 质量指标 / 响度标准化 / 数据集校验。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

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
    assert global_min < -0.9
    assert global_max > 0.9


def test_audio_metrics(sample_wav: Path) -> None:
    m = audio_metrics(sample_wav)
    assert m["duration"] == pytest.approx(3.0, abs=0.1)
    assert m["sample_rate"] == 48000
    assert m["rms_db"] < -5.0          # 正弦波 RMS 一定小于 0dBFS
    assert m["peak_db"] > -1.0         # 满幅正弦波峰值接近 0dBFS
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

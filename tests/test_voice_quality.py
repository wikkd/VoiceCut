"""清晰度打分单元测试：合成信号验证分项与排序，导出过滤/排序。"""
from __future__ import annotations

import numpy as np
import soundfile as sf

from app import dataset as dataset_mod
from app import voice_quality as vq_mod

SR = 16000


def _write(path, x: np.ndarray, sr: int = SR) -> str:
    sf.write(str(path), x.astype(np.float32), sr)
    return str(path)


def _tone(seconds: float, freq: float = 220.0, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def test_score_clean_above_noisy(tmp_path) -> None:
    """纯净谐波 + 适度停顿 > 白噪声打底的信号。"""
    clean = _tone(2.0)
    clean[int(0.8 * SR):int(1.2 * SR)] = 0.0            # 短停顿
    rng = np.random.default_rng(7)
    noisy = (_tone(2.0) * 0.4 + rng.normal(0, 0.25, int(2.0 * SR))).astype(np.float32)
    a = vq_mod.score_clip_data(clean, SR)["q"]
    b = vq_mod.score_clip_data(noisy, SR)["q"]
    assert 0.0 <= a <= 100.0 and 0.0 <= b <= 100.0
    assert a > b + 10


def test_score_clipping_penalized(tmp_path) -> None:
    good = _tone(2.0, amp=0.3)
    clipped = np.clip(_tone(2.0, amp=3.0), -0.95, 0.95)  # 大幅削波
    a = vq_mod.score_clip_data(good, SR)["q"]
    b = vq_mod.score_clip_data(clipped, SR)["q"]
    assert a > b


def test_score_silence_penalized() -> None:
    mostly_silent = np.zeros(int(2.0 * SR), dtype=np.float32)
    mostly_silent[int(1.8 * SR):] = _tone(0.2, amp=0.3)
    full = _tone(2.0, amp=0.3)
    a = vq_mod.score_clip_data(full, SR)["q"]
    b = vq_mod.score_clip_data(mostly_silent, SR)["q"]
    assert a > b + 15


def test_score_short_clip_zero() -> None:
    r = vq_mod.score_clip_data(np.zeros(int(0.01 * SR), dtype=np.float32), SR)
    assert r["q"] == 0.0


def test_score_band_labels() -> None:
    assert vq_mod.score_band(85) == "优"
    assert vq_mod.score_band(70) == "良"
    assert vq_mod.score_band(30) == "差"
    assert vq_mod.score_band(None) == ""


def test_export_min_score_filter_and_sort(tmp_path) -> None:
    """min_score 过滤低分 + list.txt 按分数降序（高分在前 = 主要素材）。"""
    src = _write(tmp_path / "s.wav", np.concatenate([_tone(12.0)]))
    segs = [
        dataset_mod.DatasetSegment(item_id="it", start=0, end=3, text="低分", score=20),
        dataset_mod.DatasetSegment(item_id="it", start=3, end=6, text="高分", score=90),
        dataset_mod.DatasetSegment(item_id="it", start=6, end=9, text="中分", score=70),
        dataset_mod.DatasetSegment(item_id="it", start=9, end=12, text="低分2", score=10),
    ]
    out = tmp_path / "ds"
    res = dataset_mod.export_dataset(src, segs, out, min_score=50)
    # 低分（20/10）被跳过
    reasons = [s["reason"] for s in res["skipped"]]
    assert sum(1 for r in reasons if "清晰度过低" in r) == 2
    # list.txt 顺序：90 分在前，70 分在后
    lines = res["list_content"].strip().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("高分") and lines[1].endswith("中分")

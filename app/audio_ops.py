"""音频分析：波形峰值、质量指标、响度处理。使用 soundfile + numpy。"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile as sf

from app.ffmpeg_util import FFmpegError, run_ffmpeg

# 波形峰值目标点数（前端 wavesurfer 渲染用，过高无意义）
PEAK_POINTS = 4000


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """读 WAV → (单声道 float32 [-1,1], sample_rate)。"""
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    # 取平均声道（简单下混）
    mono = data.mean(axis=1).astype(np.float32)
    return mono, sr


def compute_peaks(path: str | Path, max_points: int = PEAK_POINTS) -> list[list[float]]:
    """计算波形 min/max 峰值对，供前端渲染。

    流式分块读取（逐块取 min/max），避免整文件载入内存造成尖峰；
    返回 [[min, max], ...]，长度 ≤ max_points，与整段读取语义一致。
    """
    with sf.SoundFile(str(path), mode="r") as sfh:
        n = int(sfh.frames)
        if n <= 0:
            return []
        block = math.ceil(n / max_points)
        peaks: list[list[float]] = []
        while True:
            seg = sfh.read(block, dtype="float32", always_2d=False)
            if seg.size == 0:
                break
            if seg.ndim == 2:
                seg = seg.mean(axis=1)
            peaks.append([float(seg.min()), float(seg.max())])
        return peaks


# ── 质量指标（GPT-SoVITS 数据校验用）──────────────────────────

def audio_metrics(path: str | Path) -> dict:
    """计算质量指标：
    - rms_db        : 整体 RMS (dBFS)
    - peak_db       : 峰值 (dBFS)
    - silence_ratio : 静音占比 (低于 -50dBFS 的采样比例)
    - clipping      : 是否削波 (|x| > 0.999)
    - duration      : 时长(秒)
    - sample_rate   : 采样率
    """
    data, sr = read_wav(path)
    eps = 1e-9
    rms = float(np.sqrt(np.mean(data**2) + eps))
    peak = float(np.max(np.abs(data)) + eps)
    rms_db = 20.0 * math.log10(rms)
    peak_db = 20.0 * math.log10(peak)
    silence_ratio = float(np.mean(np.abs(data) < 10 ** (-50 / 20)))
    clipping = bool(np.any(np.abs(data) > 0.999))
    return {
        "rms_db": round(rms_db, 2),
        "peak_db": round(peak_db, 2),
        "silence_ratio": round(silence_ratio, 4),
        "clipping": clipping,
        "duration": round(data.shape[0] / sr, 3),
        "sample_rate": sr,
        "samples": int(data.shape[0]),
    }


def normalize_loudness(src: str | Path, dst: str | Path, target_db: float = -16.0) -> Path:
    """响度标准化到目标 RMS (dBFS)。用 ffmpeg 的 loudnorm 简化实现：
    - 单遍 I=-? 保真度更高但耗时；这里用简单 RMS 归一（线性增益），速度快、可控。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    data, sr = read_wav(src)
    rms = float(np.sqrt(np.mean(data**2) + 1e-9))
    if rms <= 0:
        raise FFmpegError("静音音频无法做响度标准化")
    gain_db = target_db - (20.0 * math.log10(rms))
    run_ffmpeg([
        "-y", "-i", str(src),
        "-af", f"volume={gain_db:.4f}dB",
        "-ar", str(sr), "-ac", "1",
        str(dst),
    ])
    return dst


def validate_dataset_clip(path: str | Path, min_dur: float = 1.0, max_dur: float = 15.0) -> dict:
    """训练片段合规性校验（GPT-SoVITS 建议 1~15s，主力 2~8s）。"""
    m = audio_metrics(path)
    issues: list[str] = []
    if m["duration"] < min_dur:
        issues.append(f"过短 (<{min_dur}s)")
    if m["duration"] > max_dur:
        issues.append(f"过长 (>{max_dur}s)")
    if m["silence_ratio"] > 0.5:
        issues.append("静音占比过高")
    if m["clipping"]:
        issues.append("存在削波")
    ok = not issues
    return {"ok": ok, "issues": issues, **m}

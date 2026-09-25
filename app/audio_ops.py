"""音频分析：波形峰值、质量指标、响度处理。使用 soundfile + numpy。

Rust 加速层（可选）：rust/vc-audio 构建出的 vc_audio 扩展与本模块接口一致；
未构建（ImportError）或设 VC_AUDIO_DISABLE=1 时自动回退纯 Python 实现，
两条路径共用 _finish_metrics 收尾，输出逐字节一致。
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import soundfile as sf

from app.ffmpeg_util import FFmpegError, run_ffmpeg

try:
    import vc_audio as _rust_audio
except ImportError:
    _rust_audio = None
if os.environ.get("VC_AUDIO_DISABLE"):
    _rust_audio = None

# 波形峰值目标点数（前端 wavesurfer 渲染用，过高无意义）
PEAK_POINTS = 4000


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """读 WAV → (单声道 float32 [-1,1], sample_rate)。"""
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    # 取平均声道（简单下混）
    mono = data.mean(axis=1).astype(np.float32)
    return mono, sr


def wav_duration(path: str | Path) -> float | None:
    """直读 wav 头取时长（秒）；失败返回 None（调用方回退 ffmpeg）。

    register_item 每次导入都会取时长，原先走 ffmpeg 子进程（~100ms/次），
    纯头部解析是微秒级。返回值与 media_duration 语义一致（完整文件时长）。
    """
    try:
        info = sf.info(str(path))
        if info.frames > 0 and info.samplerate > 0:
            return info.frames / info.samplerate
    except Exception:  # noqa: BLE001
        pass
    return None


def compute_peaks(path: str | Path, max_points: int = PEAK_POINTS) -> list[list[float]]:
    """计算波形 min/max 峰值对，供前端渲染。

    流式分块读取（逐块取 min/max），避免整文件载入内存造成尖峰；
    返回 [[min, max], ...]，长度 ≤ max_points，与整段读取语义一致。
    """
    if _rust_audio is not None:
        return _rust_audio.compute_peaks(str(path), max_points)
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

def _finish_metrics(rms: float, peak: float, silence_ratio: float, clipping: bool,
                    duration: float, sample_rate: int, samples: int) -> dict:
    """metrics 收尾（对数换算 + round），Rust/Python 两条路径共用。"""
    return {
        "rms_db": round(20.0 * math.log10(rms), 2),
        "peak_db": round(20.0 * math.log10(peak), 2),
        "silence_ratio": round(silence_ratio, 4),
        "clipping": clipping,
        "duration": round(duration, 3),
        "sample_rate": sample_rate,
        "samples": int(samples),
    }


def audio_metrics(path: str | Path) -> dict:
    """计算质量指标：
    - rms_db        : 整体 RMS (dBFS)
    - peak_db       : 峰值 (dBFS)
    - silence_ratio : 静音占比 (低于 -50dBFS 的采样比例)
    - clipping      : 是否削波 (|x| > 0.999)
    - duration      : 时长(秒)
    - sample_rate   : 采样率
    """
    if _rust_audio is not None:
        m = _rust_audio.audio_metrics_raw(str(path))
        return _finish_metrics(m["rms"], m["peak"], m["silence_ratio"],
                               m["clipping"], m["duration"],
                               m["sample_rate"], m["samples"])
    data, sr = read_wav(path)
    eps = 1e-9
    rms = float(np.sqrt(np.mean(data**2) + eps))
    peak = float(np.max(np.abs(data)) + eps)
    silence_ratio = float(np.mean(np.abs(data) < 10 ** (-50 / 20)))
    clipping = bool(np.any(np.abs(data) > 0.999))
    return _finish_metrics(rms, peak, silence_ratio, clipping,
                           data.shape[0] / sr, sr, data.shape[0])


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

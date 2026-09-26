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


def detect_speech_ranges(
    wav_path: str | Path,
    *,
    min_gap: float = 0.30,
    threshold: float = 0.5,
) -> list[tuple[float, float]]:
    """Silero VAD（faster-whisper 内置，模型随包分发）检测语音区间。

    对 BGM/环境音鲁棒——固定电平与能量阈值在 Drama CD/番剧上找不到句间
    空隙，神经 VAD 可以。返回合并后的语音区间 [(start, end), ...]（秒）。
    音频经 ffmpeg 解码重采样到 16k 单声道，全程 O(音频时长) 内存。
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    from app.speakers import read_mono16k

    mono, sr = read_mono16k(wav_path)
    opts = VadOptions(threshold=threshold,
                      min_silence_duration_ms=int(min_gap * 1000),
                      min_speech_duration_ms=250,
                      speech_pad_ms=60)
    ts = get_speech_timestamps(mono, opts, sampling_rate=sr)
    return [(round(d["start"] / sr, 3), round(d["end"] / sr, 3)) for d in ts]


def detect_silence_adaptive(
    wav_path: str | Path,
    *,
    min_silence: float = 0.30,
    merge_gap: float = 0.12,
) -> list[tuple[float, float]]:
    """能量自适应静音检测（Silero 不可用时的兜底）。

    按整段音频的帧能量分布自动确定"静音"电平：最轻 10% 帧 ≈ 环境音电平
    lo，最响 10% ≈ 语音峰 hi；动态范围 hi-lo < 8dB 时无可用对比度返回 []；
    阈值 = lo + max(6, 0.20*(hi-lo)) dB。返回 [(start, end), ...] 静音区间。
    """
    mono, sr = read_wav(wav_path)
    dec = max(1, sr // 16000)
    if dec > 1:
        mono = mono[::dec]
        sr = sr // dec
    n = mono.size
    if n < int(0.5 * sr):
        return []
    fl = max(1, int(0.050 * sr))   # 帧长 50ms
    hp = max(1, int(0.020 * sr))   # 帧移 20ms
    sq = np.cumsum(mono.astype(np.float64) ** 2)
    starts = np.arange(0, n - fl + 1, hp)
    sums = sq[starts + fl] - sq[starts]
    rms = np.sqrt(sums / fl + 1e-12)
    db = 20.0 * np.log10(rms + 1e-12)
    k = max(1, int(db.size * 0.10))
    srt = np.sort(db)
    lo = float(np.mean(srt[:k]))
    hi = float(np.mean(srt[-k:]))
    if hi - lo < 8.0:
        return []
    thr = lo + max(6.0, 0.20 * (hi - lo))
    silent = db < thr
    frame_sec = hp / sr
    raw: list[tuple[float, float]] = []
    m = silent.size
    i = 0
    while i < m:
        if silent[i]:
            j = i
            while j + 1 < m and silent[j + 1]:
                j += 1
            raw.append((i * frame_sec, (j + 1) * frame_sec + fl / sr))
            i = j + 1
        else:
            i += 1
    merged: list[tuple[float, float]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    dur = n / sr
    return [(round(s, 3), round(min(e, dur), 3))
            for s, e in merged if e - s >= min_silence]

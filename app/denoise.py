"""一键降噪：noisereduce（保守参数，避免损伤音色）。"""
from __future__ import annotations

from pathlib import Path

import noisereduce as nr
import numpy as np
import soundfile as sf

from app.audio_ops import read_wav


def denoise_wav(
    src: str | Path,
    dst: str | Path,
    *,
    stationary: bool = True,
    prop_decrease: float = 0.75,
    sample_rate: int = 48000,
) -> Path:
    """对 WAV 执行降噪并写出新 WAV。

    - stationary=True : 用整段统计噪声谱（适合稳定底噪，动漫/广播剧默认）
    - prop_decrease   : 降噪强度，默认 0.75 保守值
    - 注意：过度降噪会损伤音色，训练数据宁可保留轻微底噪
    """
    data, sr = read_wav(src)
    if sr != sample_rate:
        # 重采样到目标采样率再处理（noisereduce 对任意 sr 均可用，这里统一工作采样率）
        import scipy.signal

        data = scipy.signal.resample_poly(data, sample_rate, sr).astype(np.float32)
        sr = sample_rate

    y = nr.reduce_noise(
        y=data,
        sr=sr,
        stationary=stationary,
        prop_decrease=prop_decrease,
        n_fft=2048,
        n_jobs=1,
    )
    y = np.clip(y, -1.0, 1.0).astype(np.float32)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), y, sr, subtype="PCM_16")
    return dst

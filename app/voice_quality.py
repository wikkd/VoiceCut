"""人声清晰度打分：无需参考音频的无参考质量评估（0~100 分）。

打分维度与权重（均为片段级、纯 numpy 实现，GPU 不参与）：
    SNR 估计     40 分  高能量帧(语音) vs 低能量帧(底噪) 的能量比；
                        无明显低能量帧（连续语音）时给中性 0.85 不奖不罚
    削波         20 分  |x|>=0.99 采样占比，>0.1% 开始扣，>1% 扣满
    电平合适度   15 分  RMS 过低（底噪相对突出）/ 过响（接近满幅）都扣分
    语音活跃度   15 分  静音占比过高（大段空白）扣分
    谱清晰度     10 分  谱平坦度（SFM）：谐波结构明显的语音分高，
                        BGM/宽带噪声平坦度高则分低

分数语义：>=80 优（可作主力训练素材）、60~79 良、<60 差（建议修复或剔除）。
打分只读音频、不修改片段内容，对 locked 片段同样适用。
"""
from __future__ import annotations

import math

import numpy as np

_FRAME_SEC = 0.032      # 帧长 32ms
_HOP_SEC = 0.008        # 帧移 8ms
_EPS = 1e-9


def _frame_rms(mono: np.ndarray, sr: int) -> np.ndarray:
    """分帧 RMS（末尾不足一帧丢弃）。"""
    fl = max(1, int(_FRAME_SEC * sr))
    hp = max(1, int(_HOP_SEC * sr))
    if mono.size < fl:
        return np.array([float(np.sqrt(np.mean(mono**2) + _EPS))])
    n = 1 + (mono.size - fl) // hp
    idx = np.arange(fl)[None, :] + hp * np.arange(n)[:, None]
    frames = mono[idx]
    return np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + _EPS)


def _snr_component(mono: np.ndarray, sr: int, silence_ratio: float) -> tuple[float, float]:
    """SNR 分量 (0~1) 与估计的 snr_db。"""
    rms = _frame_rms(mono, sr)
    db = 20.0 * np.log10(rms + _EPS)
    n = db.size
    if n < 8:
        return 0.85, 20.0
    k = max(1, int(n * 0.10))
    srt = np.sort(db)
    hi = float(np.mean(srt[-k:]))          # 最响 10% 帧 ≈ 语音峰
    lo = float(np.mean(srt[:k]))           # 最轻 10% 帧 ≈ 底噪
    snr_db = hi - lo
    # 无明显低能量帧（底噪帧能量接近语音均值）：snr_db 反映的是音量动态而非
    # 信噪比，此时不惩罚——给中性 0.85。
    if silence_ratio < 0.02 or lo > (float(np.mean(db)) - 6.0):
        return 0.85, round(snr_db, 1)
    return max(0.0, min(1.0, snr_db / 30.0)), round(snr_db, 1)


def _clip_component(mono: np.ndarray) -> tuple[float, float]:
    """削波分量 (0~1) 与削波采样占比。"""
    ratio = float(np.mean(np.abs(mono) >= 0.99))
    if ratio <= 0.001:
        score = 1.0
    elif ratio >= 0.01:
        score = 0.0
    else:  # 0.1% ~ 1% 线性下降
        score = 1.0 - (ratio - 0.001) / 0.009
    return score, ratio


def _level_component(mono: np.ndarray) -> tuple[float, float]:
    """电平分量 (0~1) 与 rms_db。-24~-10dBFS 满分，两侧衰减。"""
    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2) + _EPS))
    rms_db = 20.0 * math.log10(rms + _EPS)
    if -24.0 <= rms_db <= -10.0:
        score = 1.0
    elif -34.0 <= rms_db < -24.0:          # 过轻
        score = 1.0 - (-24.0 - rms_db) / 10.0 * 0.7
    elif -10.0 < rms_db <= -4.0:           # 偏响
        score = 1.0 - (rms_db + 10.0) / 6.0 * 0.5
    else:                                   # 极轻(<-34)/极响(>-4)
        score = 0.15
    return max(0.0, min(1.0, score)), round(rms_db, 1)


def _activity_component(mono: np.ndarray, sr: int) -> tuple[float, float]:
    """活跃度分量 (0~1) 与静音占比（低于 -50dBFS 的采样比例）。"""
    silence = float(np.mean(np.abs(mono) < 10 ** (-50.0 / 20.0)))
    if silence <= 0.30:
        score = 1.0
    elif silence >= 0.70:
        score = 0.0
    else:
        score = 1.0 - (silence - 0.30) / 0.40
    return score, round(silence, 4)


def _spectral_component(mono: np.ndarray, sr: int) -> float:
    """谱清晰度分量 (0~1)：谱平坦度 SFM 越低（谐波结构越强）分越高。"""
    x = mono.astype(np.float64)
    if x.size < 2048:
        return 0.7  # 片段太短无谱信息，中性
    fl = int(0.032 * sr)
    hp = int(0.008 * sr)
    n = 1 + (x.size - fl) // hp
    if n < 4:
        return 0.7
    idx = np.arange(fl)[None, :] + hp * np.arange(n)[:, None]
    frames = x[idx] * np.hanning(fl)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1)) + _EPS
    sfm = float(np.mean(np.exp(np.mean(np.log(spec), axis=1))
                        / (np.mean(spec, axis=1) + _EPS)))
    # 语音典型 SFM ≈ 0.005~0.05，宽带噪声 ≈ 0.3+；0.05 以下满分，0.5 以上 0 分
    if sfm <= 0.05:
        score = 1.0
    elif sfm >= 0.50:
        score = 0.0
    else:
        score = 1.0 - (sfm - 0.05) / 0.45
    return max(0.0, min(1.0, score))


def score_clip_data(mono: np.ndarray, sr: int) -> dict:
    """对单声道 float [-1,1] 音频计算清晰度分数（0~100）及分项。"""
    mono = np.asarray(mono, dtype=np.float32)
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    if mono.size < int(0.05 * sr):
        return {"q": 0.0, "snr_db": None, "clip_ratio": 0.0,
                "rms_db": -120.0, "silence_ratio": 1.0}
    snr_c, snr_db = _snr_component(mono, sr, float(np.mean(np.abs(mono) < 10 ** (-50.0 / 20.0))))
    clip_c, clip_ratio = _clip_component(mono)
    level_c, rms_db = _level_component(mono)
    act_c, silence_ratio = _activity_component(mono, sr)
    spec_c = _spectral_component(mono, sr)
    q = 40.0 * snr_c + 20.0 * clip_c + 15.0 * level_c + 15.0 * act_c + 10.0 * spec_c
    return {
        "q": round(q, 1),
        "snr_db": snr_db,
        "clip_ratio": round(clip_ratio, 5),
        "rms_db": rms_db,
        "silence_ratio": silence_ratio,
    }


def score_clip_wav(wav_path, start: float, end: float) -> dict:
    """读取 wav 的 [start, end) 区间并打分。"""
    from app.audio_ops import read_wav

    data, sr = read_wav(wav_path)
    s = max(0, int(start * sr))
    e = min(int(end * sr), data.shape[0])
    if e <= s:
        return {"q": 0.0, "snr_db": None, "clip_ratio": 0.0,
                "rms_db": -120.0, "silence_ratio": 1.0}
    return score_clip_data(data[s:e], sr)


def score_band(q: float | None) -> str:
    """分数档位：优 / 良 / 差（前端展示与导出过滤用）。"""
    if q is None:
        return ""
    if q >= 80.0:
        return "优"
    if q >= 60.0:
        return "良"
    return "差"

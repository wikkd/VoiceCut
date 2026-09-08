"""环境冒烟测试：依赖导入 / CUDA(Blackwell) / 关键库可用性。"""
from __future__ import annotations

from pathlib import Path

import pytest


def test_core_imports() -> None:
    import flask
    import numpy
    import scipy
    import soundfile
    import noisereduce
    import yt_dlp
    import faster_whisper
    import torch
    import torchaudio

    assert flask.__version__ >= "3"
    assert noisereduce.__version__  # noqa: B009

    # demucs 依赖的音频库
    import demucs  # noqa: F401
    assert demucs.__version__  # noqa: B009


def test_ffmpeg_found() -> None:
    from app.config import find_ffmpeg

    p = find_ffmpeg()
    assert p.lower().endswith("ffmpeg.exe"), p
    # 配套 ffprobe 存在
    assert Path(p).with_name("ffprobe.exe").exists()


def test_torch_cuda_blackwell() -> None:
    """GPU 校验：RTX 5060 Ti (Blackwell sm_120) 应可用。"""
    import torch

    if not torch.cuda.is_available():
        pytest.skip("CUDA 不可用（CPU 环境，跳过 GPU 校验）")

    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    print(f"\n  GPU: {name}  capability={cap}  torch_cuda={torch.version.cuda}")
    # Blackwell = (12, x)。宽松断言：至少 CUDA 可用且算力 >= (8,0)
    assert cap[0] >= 8, f"算力过低: {cap}"

    # 实际跑一次 GPU 张量运算确认可用
    a = torch.rand(256, 256, device="cuda")
    b = torch.matmul(a, a)
    torch.cuda.synchronize()
    assert b.shape == (256, 256)

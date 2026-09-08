"""pytest 公共夹具：用 ffmpeg 合成测试音频/视频。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import find_ffmpeg


def _ffmpeg() -> str:
    return find_ffmpeg()


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def sample_wav(media_dir: Path) -> Path:
    """3 秒 440Hz 正弦波 WAV (48kHz 单声道)。"""
    wav = media_dir / "sine_440.wav"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-ar", "48000", "-ac", "1", str(wav)],
        check=True, capture_output=True,
    )
    return wav


@pytest.fixture(scope="session")
def sample_video(media_dir: Path) -> Path:
    """3 秒测试视频 MP4 (H.264 + AAC)。"""
    mp4 = media_dir / "testsrc.mp4"
    subprocess.run(
        [_ffmpeg(), "-y",
         "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         "-shortest", str(mp4)],
        check=True, capture_output=True,
    )
    return mp4

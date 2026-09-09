"""ffmpeg_util 单元测试：探测 / 抽流 / 导出 / 去静音 / 预览转封装。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ffmpeg_util import (
    extract_audio,
    export_segment,
    media_duration,
    probe,
    remux_preview,
    trim_silence,
)


def test_probe(sample_wav: Path) -> None:
    info = probe(sample_wav)
    assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.1)


def test_media_duration(sample_wav: Path, sample_video: Path) -> None:
    assert media_duration(sample_wav) == pytest.approx(3.0, abs=0.1)
    assert media_duration(sample_video) == pytest.approx(3.0, abs=0.1)


def test_extract_audio_from_video(sample_video: Path, tmp_path: Path) -> None:
    out = extract_audio(sample_video, tmp_path / "audio.wav", sample_rate=48000)
    assert out.exists()
    dur = media_duration(out)
    assert dur == pytest.approx(3.0, abs=0.15)


def test_export_segment_wav(sample_wav: Path, tmp_path: Path) -> None:
    out = export_segment(sample_wav, tmp_path / "cut.wav", 0.5, 1.75, sample_rate=32000)
    assert out.exists()
    assert media_duration(out) == pytest.approx(1.25, abs=0.1)


def test_export_segment_mp3(sample_wav: Path, tmp_path: Path) -> None:
    out = export_segment(sample_wav, tmp_path / "cut.mp3", 0.0, 1.0,
                         sample_rate=44100, codec="libmp3lame")
    assert out.exists()
    assert out.suffix == ".mp3"


def test_export_segment_invalid(sample_wav: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        export_segment(sample_wav, tmp_path / "bad.wav", 2.0, 1.0, sample_rate=32000)


def test_trim_silence(media_dir: Path, tmp_path: Path) -> None:
    """生成 1s 静音 + 1s 正弦 + 1s 静音，去头尾后应 ≈1s。"""
    import subprocess

    from app.config import find_ffmpeg

    src = media_dir / "padded.wav"
    subprocess.run(
        [find_ffmpeg(), "-y",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=1",
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1",
         "-ar", "48000", "-ac", "1", str(src)],
        check=True, capture_output=True,
    )
    out = trim_silence(src, tmp_path / "trimmed.wav", sample_rate=48000)
    dur = media_duration(out)
    # 原 3s → 去头尾静音后应接近 1s（含 pad 0.05s）
    assert 0.9 < dur < 1.3, f"去静音后时长异常: {dur}"


def test_remux_preview(sample_video: Path, tmp_path: Path) -> None:
    out = remux_preview(sample_video, tmp_path / "preview.mp4")
    assert out.exists()
    info = probe(out)
    codecs = {s.get("codec_name") for s in info["streams"]}
    assert "h264" in codecs or "hevc" in codecs


def test_detect_silence_parses_stderr(monkeypatch, tmp_path) -> None:
    from app import ffmpeg_util as fu

    class _Proc:
        stderr = (
            "[silencedetect @ 0x1] silence_start: 1.5\n"
            "[silencedetect @ 0x1] silence_end: 2.5 | silence_duration: 1\n"
            "[silencedetect @ 0x1] silence_start: 8.0\n"
        )

    def fake_run(args, **kw):
        return _Proc()

    monkeypatch.setattr(fu, "run_ffmpeg", fake_run)
    out = fu.detect_silence(tmp_path / "x.wav", threshold_db=-40.0, min_silence=0.4)
    assert out == [(1.5, 2.5), (8.0, float("inf"))]

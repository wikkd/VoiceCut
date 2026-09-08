"""ffmpeg 进程封装：探测、运行、转码、抽流。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.config import find_ffmpeg


class FFmpegError(RuntimeError):
    """ffmpeg 执行失败。"""


def _ffmpeg() -> str:
    return find_ffmpeg()


def run_ffmpeg(
    args: list[str],
    *,
    capture: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """执行 ffmpeg/ffprobe 命令。

    - `capture=True`: 捕获输出, 失败抛 FFmpegError(含 stderr 尾部)。
    - `capture=False`: 直接继承流 (适合长任务, 便于外层读取进度)。
    """
    cmd = [_ffmpeg(), *args]
    if capture:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-12:]
            raise FFmpegError(f"ffmpeg 失败 (rc={proc.returncode}):\n" + "\n".join(tail))
        return proc
    return subprocess.Popen(cmd)


def probe(path: str | Path) -> dict:
    """返回媒体文件的 ffprobe JSON 元信息。"""
    proc = subprocess.run(
        [str(Path(_ffmpeg()).with_name("ffprobe.exe")), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 失败: {(proc.stderr or '').strip()[:500]}")
    return json.loads(proc.stdout)


def media_duration(path: str | Path) -> float:
    """媒体时长（秒）。"""
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        # 回退：从音频/视频流读取时长
        for st in info.get("streams", []):
            d = st.get("duration")
            if d:
                return float(d)
        raise FFmpegError("无法读取媒体时长")


def extract_audio(
    src: str | Path,
    dst: str | Path,
    *,
    sample_rate: int = 48000,
    channels: int = 1,
    codec: str = "pcm_s16le",
) -> Path:
    """把任意媒体（视频/音频）转成 PCM WAV，供波形计算与处理。

    - 默认 48kHz 单声道 16bit PCM（内部工作格式）。
    - 训练集导出再统一重采样到 32kHz。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-i", str(src),
        "-vn", "-acodec", codec,
        "-ar", str(sample_rate), "-ac", str(channels),
        str(dst),
    ])
    return dst


def remux_preview(src: str | Path, dst: str | Path) -> Path:
    """把视频无损转封装为浏览器可播的 MP4 (H.264/AAC)。

    `-c copy` 失败时回退转码。用于页面内视频预览。
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg(["-y", "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(dst)])
    except FFmpegError:
        run_ffmpeg([
            "-y", "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst),
        ])
    return dst


def export_segment(
    src: str | Path,
    dst: str | Path,
    start: float,
    end: float,
    *,
    sample_rate: int,
    codec: str = "pcm_s16le",
    channels: int = 1,
) -> Path:
    """导出选区 [start, end) 秒为音频文件。

    - codec="pcm_s16le" → WAV; codec="libmp3lame" → MP3。
    - `-ss` 放在 `-i` 前（输入定位，快）。
    """
    dst = Path(dst)
    dur = max(0.0, end - start)
    if dur <= 0:
        raise ValueError(f"无效选区: start={start} end={end}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-ss", f"{start:.6f}", "-i", str(src),
        "-t", f"{dur:.6f}",
        "-vn", "-acodec", codec,
        "-ar", str(sample_rate), "-ac", str(channels),
        str(dst),
    ])
    return dst


def trim_silence(
    src: str | Path,
    dst: str | Path,
    *,
    sample_rate: int,
    silence_threshold: str = "-35dB",
    min_silence: float = 0.10,
    pad: float = 0.05,
) -> Path:
    """去除头尾静音（ffmpeg silenceremove + 反向再处理一次）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    pad_s = f"{pad:.3f}"
    run_ffmpeg([
        "-y", "-i", str(src),
        "-af",
        f"silenceremove=start_periods=1:start_threshold={silence_threshold}:start_silence={min_silence:.3f},"
        f"areverse,silenceremove=start_periods=1:start_threshold={silence_threshold}:start_silence={min_silence:.3f},"
        f"areverse,apad=pad_dur={pad_s}:pad_dur_type=end",
        "-ar", str(sample_rate), "-ac", "1",
        str(dst),
    ])
    return dst

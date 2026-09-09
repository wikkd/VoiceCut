"""ffmpeg 进程封装：探测、运行、转码、抽流。

probe() 优先使用 ffprobe（JSON）；若环境无 ffprobe（如 D:\\ffmpeg 只有 ffmpeg.exe），
回退到解析 `ffmpeg -i` 的 stderr。
"""
from __future__ import annotations

import json
import re
import shutil
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-12:]
            raise FFmpegError(f"ffmpeg 失败 (rc={proc.returncode}):\n" + "\n".join(tail))
        return proc
    return subprocess.Popen(cmd)


# ── 探测 ─────────────────────────────────────────────────────

_FFPROBE: str | None = None


def _ffprobe_path() -> str | None:
    """ffprobe 路径：与 ffmpeg 同目录优先，其次 PATH。找不到返回 None。"""
    global _FFPROBE
    if _FFPROBE is None:
        p = Path(_ffmpeg()).with_name("ffprobe.exe")
        if p.exists():
            _FFPROBE = str(p)
        else:
            q = shutil.which("ffprobe")
            _FFPROBE = q
    return _FFPROBE


def probe(path: str | Path) -> dict:
    """返回媒体文件元信息 {"format": {...}, "streams": [...]}。"""
    fp = _ffprobe_path()
    if fp:
        proc = subprocess.run(
            [fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            info = json.loads(proc.stdout)
            if info.get("format") or info.get("streams"):
                return info
    return _probe_via_ffmpeg(path)


def _probe_via_ffmpeg(path: str | Path) -> dict:
    """无 ffprobe 时，用 `ffmpeg -i` 的 stderr 解析基本元信息。"""
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", str(path), "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    err = proc.stderr or ""

    duration: float | None = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        hh, mm, ss = m.groups()
        duration = float(hh) * 3600 + float(mm) * 60 + float(ss)

    fmt_name: str | None = None
    m = re.search(r"Input #\d+, ([^,]+), from '", err)
    if m:
        fmt_name = m.group(1).strip()

    streams: list[dict] = []
    for line in err.splitlines():
        m = re.search(r"Stream #\S+: (Video|Audio): (\S+)", line)
        if not m:
            continue
        st = {"codec_type": m.group(1).lower(), "codec_name": m.group(2)}
        streams.append(st)

    if duration is None:
        raise FFmpegError(f"无法解析媒体信息: {(err or 'empty')[:300]}")
    return {"format": {"duration": duration, "format_name": fmt_name}, "streams": streams}


def media_duration(path: str | Path) -> float:
    """媒体时长（秒）。"""
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        for st in info.get("streams", []):
            d = st.get("duration")
            if d:
                return float(d)
        raise FFmpegError("无法读取媒体时长")


# ── 抽流 / 导出 / 处理 ───────────────────────────────────────

def extract_audio(
    src: str | Path,
    dst: str | Path,
    *,
    sample_rate: int = 48000,
    channels: int = 1,
    codec: str = "pcm_s16le",
) -> Path:
    """把任意媒体（视频/音频）转成 PCM WAV，供波形计算与处理。"""
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
    """把视频无损转封装为浏览器可播的 MP4 (H.264/AAC)。失败回退转码。"""
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
    run_ffmpeg([
        "-y", "-i", str(src),
        "-af",
        f"silenceremove=start_periods=1:start_threshold={silence_threshold}:start_silence={min_silence:.3f},"
        f"areverse,silenceremove=start_periods=1:start_threshold={silence_threshold}:start_silence={min_silence:.3f},"
        f"areverse",
        "-ar", str(sample_rate), "-ac", "1",
        str(dst),
    ])
    return dst

def export_segment_trimmed(
    src: str | Path,
    dst: str | Path,
    start: float,
    end: float,
    *,
    sample_rate: int,
    silence_threshold: str = "-35dB",
    min_silence: float = 0.10,
) -> Path:
    """Cut [start, end) and trim head/tail silence in a single ffmpeg call.

    Equivalent to export_segment() followed by trim_silence() but spawns only
    one ffmpeg process (used by the GPT-SoVITS dataset exporter).
    """
    dst = Path(dst)
    dur = max(0.0, end - start)
    if dur <= 0:
        raise ValueError(f"invalid segment: start={start} end={end}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([
        "-y", "-ss", f"{start:.6f}", "-i", str(src),
        "-t", f"{dur:.6f}",
        "-af",
        f"silenceremove=start_periods=1:start_threshold={silence_threshold}:start_silence={min_silence:.3f},"
        f"areverse,silenceremove=start_periods=1:start_threshold={silence_threshold}:start_silence={min_silence:.3f},"
        f"areverse",
        "-ar", str(sample_rate), "-ac", "1",
        str(dst),
    ])
    return dst


def detect_silence(
    src: str | Path,
    *,
    threshold_db: float = -35.0,
    min_silence: float = 0.5,
) -> list[tuple[float, float]]:
    """Detect silence intervals with ffmpeg's silencedetect filter.

    Parses ``silence_start:`` / ``silence_end:`` lines from stderr. A trailing
    silence that runs to EOF has no ``silence_end``; its end is set to +inf so
    callers can clamp to the media duration.
    """
    proc = run_ffmpeg([
        "-hide_banner", "-nostats", "-i", str(src),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence:.3f}",
        "-f", "null", "-",
    ])
    err = proc.stderr or ""
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", err)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", err)]
    out: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else float("inf")
        if e > s:
            out.append((s, e))
    return out

"""Subtitle handling: SRT/ASS parsing, embedded-subtitle extraction via ffmpeg, SRT writing.

Used by the real-time subtitle panel: video imports try to auto-extract an embedded
subtitle track; users may also upload an external .srt/.ass file or generate timed
subtitles with Whisper.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.ffmpeg_util import FFmpegError, run_ffmpeg

# SRT time: 00:00:01,000 (milliseconds); ASS time: 0:00:01.50 (centiseconds)
_TS_PAIR_RE = re.compile(
    r"(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,3}:\d{2}:\d{2}[,.]\d{1,3})"
)
_ASS_DIALOGUE_RE = re.compile(r"^Dialogue:\s*(.*)$", re.MULTILINE)
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")


@dataclass
class SubtitleLine:
    """One subtitle line: start/end seconds + plain text."""

    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


def _parse_ts(ts: str, *, scale: int) -> float:
    """Parse H:MM:SS.frac to seconds; frac uses `scale` (SRT=1000, ASS=100)."""
    parts = ts.split(":")
    h, m = int(parts[0]), int(parts[1])
    sec_frac = parts[2].split(".")
    sec = int(sec_frac[0])
    frac = int(sec_frac[1]) if len(sec_frac) > 1 else 0
    return h * 3600 + m * 60 + sec + frac / float(scale)


def parse_srt(text: str) -> list[SubtitleLine]:
    """Parse SRT text into a list of SubtitleLine."""
    out: list[SubtitleLine] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if lines[0].isdigit():  # index line
            lines = lines[1:]
        if not lines:
            continue
        m = _TS_PAIR_RE.search(lines[0])
        if not m:
            continue
        start = _parse_ts(m.group(1).replace(",", "."), scale=1000)
        end = _parse_ts(m.group(2).replace(",", "."), scale=1000)
        text = " ".join(lines[1:]).strip()
        if text:
            out.append(SubtitleLine(start=start, end=end, text=text))
    return out


def parse_ass(text: str) -> list[SubtitleLine]:
    """Parse ASS/SSA text: read [Events] Dialogue lines and strip override tags."""
    out: list[SubtitleLine] = []
    for m in _ASS_DIALOGUE_RE.finditer(text):
        fields = m.group(1).split(",", 9)
        if len(fields) < 10:
            continue
        start = _parse_ts(fields[1], scale=100)
        end = _parse_ts(fields[2], scale=100)
        content = _ASS_TAG_RE.sub("", fields[9])
        content = content.replace("\\N", " ").replace("\\n", " ").strip()
        if content:
            out.append(SubtitleLine(start=start, end=end, text=content))
    return out


def parse_subtitle_file(path: str | Path) -> list[SubtitleLine]:
    """Parse a subtitle file by extension; empty list if missing/unparsable."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() in (".ass", ".ssa"):
        return parse_ass(text)
    return parse_srt(text)


def extract_embedded_subtitles(src: str | Path, dst: str | Path) -> Path | None:
    """Extract the first subtitle stream to SRT via ffmpeg; None if absent/failed."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg(["-y", "-i", str(src), "-map", "0:s:0", "-c:s", "srt", str(dst)])
    except FFmpegError:
        return None
    if not dst.exists() or dst.stat().st_size == 0:
        return None
    if not parse_srt(dst.read_text(encoding="utf-8", errors="replace")):
        return None
    return dst


def _fmt_srt_ts(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: str | Path, subs: list) -> Path:
    """Write SubtitleLine/dict list to an SRT file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, s in enumerate(subs, 1):
        start = float(s.start) if hasattr(s, "start") else float(s["start"])
        end = float(s.end) if hasattr(s, "end") else float(s["end"])
        text = s.text if hasattr(s, "text") else s["text"]
        lines.append(str(i))
        lines.append(f"{_fmt_srt_ts(start)} --> {_fmt_srt_ts(end)}")
        lines.append((text or "").strip())
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return p

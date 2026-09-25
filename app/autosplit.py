"""Automatic clip splitting by silence (pure functions, no I/O).

Used by POST /api/items/<id>/autosplit: detect silence with ffmpeg, then map
the media timeline into speech clips suitable for the GPT-SoVITS training set.
"""
from __future__ import annotations

import math


def split_by_silence(
    duration: float,
    silences: list[tuple[float, float]],
    *,
    min_len: float = 0.8,
    max_len: float = 15.0,
) -> list[tuple[float, float]]:
    """Turn silence intervals into non-silent clip ranges [start, end).

    - Silences are clamped to [0, duration]; a +inf end means "to EOF".
    - Too-short speech gaps are merged forward (the intervening silence is
      included in the merged clip); a trailing run still too short is dropped.
    - Over-long speech runs are split into equal chunks of at most max_len.
    """
    dur = max(0.0, duration or 0.0)
    if dur <= 0:
        return []
    max_len = max(min_len, max_len or 0.0)

    clamped: list[tuple[float, float]] = []
    for s, e in silences:
        a = max(0.0, s)
        b = min(dur, e if math.isfinite(e) else dur)
        if b > a:
            clamped.append((a, b))

    # Non-silent speech ranges between silence gaps.
    speech: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in clamped:
        if a > cur:
            speech.append((cur, a))
        cur = max(cur, b)
    if cur < dur:
        speech.append((cur, dur))

    out: list[tuple[float, float]] = []
    pending: tuple[float, float] | None = None
    for s, e in speech:
        if pending is None:
            pending = (s, e)
        else:
            pending = (pending[0], e)  # merge forward, spanning the silence
        length = pending[1] - pending[0]
        if length < min_len:
            continue  # keep accumulating
        if length > max_len:
            n = max(1, math.ceil(length / max_len))
            chunk = length / n
            for k in range(n):
                out.append((pending[0] + k * chunk, pending[0] + (k + 1) * chunk))
            pending = None
        else:
            out.append(pending)
            pending = None
    # trailing too-short accumulation is dropped
    return out


def speech_ranges(duration: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Inverse of the silence intervals within [0, duration]: speech ranges."""
    dur = max(0.0, duration or 0.0)
    if dur <= 0:
        return []
    clamped: list[tuple[float, float]] = []
    for s, e in silences:
        a = max(0.0, s)
        b = min(dur, e if math.isfinite(e) else dur)
        if b > a:
            clamped.append((a, b))
    out: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in clamped:
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < dur:
        out.append((cur, dur))
    return out


def align_segments_to_speech(
    segments: list[dict],
    duration: float,
    speech: list[tuple[float, float]],
    *,
    pad: float = 0.08,
    min_keep: float = 0.2,
) -> int:
    """Shrink each segment window to the actual speech inside it.

    字幕时间轴是"链式"的（上句结束 = 下句开始，为阅读体验而非语音边界），
    直接按字幕切片段会带大量静音。``speech`` 为语音区间列表（Silero VAD
    或 energy-VAD 的输出）。每个片段窗口收缩到窗口内的语音区间：start 只能
    后移、end 只能前移（绝不外扩、绝不越界）；窗口内没有语音的片段保持
    原样；``locked`` 片段（人工成果）跳过。返回改动的片段数。
    """
    dur = max(0.0, duration or 0.0)
    ranges = [(max(0.0, a), min(dur, b)) for a, b in speech if b > a] if dur else []
    changed = 0
    for seg in segments:
        if seg.get("locked"):
            continue
        s = float(seg.get("start") or 0.0)
        e = float(seg.get("end") or 0.0)
        if e <= s:
            continue
        inner = [(a, b) for a, b in ranges if a < e and b > s]
        if not inner:
            continue
        first = min(a for a, _ in inner)
        last = max(b for _, b in inner)
        ns = round(max(s, first - pad), 3)
        ne = round(min(e, last + pad), 3)
        if (ns > s or ne < e) and (ne - ns) >= min_keep:
            seg["start"], seg["end"] = ns, ne
            changed += 1
    return changed

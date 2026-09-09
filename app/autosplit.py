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

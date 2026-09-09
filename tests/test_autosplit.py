"""split_by_silence unit tests (app/autosplit.py)."""
from __future__ import annotations

from app.autosplit import split_by_silence


def test_no_silence_single_clip() -> None:
    assert split_by_silence(10.0, []) == [(0.0, 10.0)]


def test_simple_split() -> None:
    # silence 2..3 and 6..7 -> speech 0..2, 3..6, 7..10
    out = split_by_silence(10.0, [(2.0, 3.0), (6.0, 7.0)])
    assert out == [(0.0, 2.0), (3.0, 6.0), (7.0, 10.0)]


def test_leading_and_trailing_silence() -> None:
    assert split_by_silence(10.0, [(0.0, 2.0), (8.0, 10.0)]) == [(2.0, 8.0)]


def test_unclosed_trailing_silence() -> None:
    assert split_by_silence(10.0, [(7.0, float("inf"))]) == [(0.0, 7.0)]


def test_too_short_merged_forward() -> None:
    # speech 0..0.5 + 0.7..3.0 with min_len 1.0 -> merged into 0..3.0
    out = split_by_silence(3.0, [(0.5, 0.7)], min_len=1.0)
    assert out == [(0.0, 3.0)]


def test_trailing_too_short_dropped() -> None:
    # 0..3.0 valid, 3.5..4.0 too short -> trailing run dropped
    out = split_by_silence(4.0, [(3.0, 3.5)], min_len=1.0)
    assert out == [(0.0, 3.0)]


def test_overlong_split_into_chunks() -> None:
    out = split_by_silence(10.0, [], min_len=0.8, max_len=4.0)
    assert len(out) == 3
    assert out[0][0] == 0.0 and out[-1][1] == 10.0
    assert all(e - s <= 4.0 + 1e-9 for s, e in out)


def test_all_silence_empty() -> None:
    assert split_by_silence(5.0, [(0.0, 5.0)]) == []


def test_zero_duration_empty() -> None:
    assert split_by_silence(0.0, []) == []


def test_clamped_out_of_range_silence() -> None:
    # silence outside [0, duration] is clamped/dropped
    assert split_by_silence(4.0, [(-1.0, 0.5), (3.5, 99.0)]) == [(0.5, 3.5)]

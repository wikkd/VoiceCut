"""空白区补扫单元测试：覆盖补集 / 区间求交（纯函数），不依赖模型与音频。"""
from __future__ import annotations

from app.web.projects import _clip_ranges, _gap_ranges


def test_gap_ranges_basic() -> None:
    segs = [{"start": 1.0, "end": 3.0}, {"start": 5.0, "end": 7.0}]
    assert _gap_ranges(segs, 10.0) == [(0.0, 1.0), (3.0, 5.0), (7.0, 10.0)]


def test_gap_ranges_empty_and_full() -> None:
    assert _gap_ranges([], 5.0) == [(0.0, 5.0)]      # 无片段 → 全片都是空白
    assert _gap_ranges([{"start": 0.0, "end": 10.0}], 10.0) == []   # 全被覆盖


def test_gap_ranges_ignores_tiny_slots() -> None:
    # 片段间 0.2s 缝隙 < min_gap 0.25 → 忽略，避免碎片片段
    segs = [{"start": 0.0, "end": 2.0}, {"start": 2.2, "end": 4.0}]
    assert _gap_ranges(segs, 6.0) == [(4.0, 6.0)]


def test_gap_ranges_unsorted_and_overlapping() -> None:
    # 乱序输入：先排序再按时间推进算补集，不重复也不遗漏（0.5~3 与 4~6 之间的 3~4 是空隙）
    segs = [{"start": 4.0, "end": 6.0}, {"start": 0.5, "end": 3.0}]
    assert _gap_ranges(segs, 8.0) == [(0.0, 0.5), (3.0, 4.0), (6.0, 8.0)]


def test_clip_ranges_intersection() -> None:
    gaps = [(0.0, 2.0), (4.0, 10.0)]
    speech = [(0.5, 1.5), (6.0, 8.0), (9.5, 12.0)]
    assert _clip_ranges(gaps, speech) == [(0.5, 1.5), (6.0, 8.0), (9.5, 10.0)]


def test_clip_ranges_no_overlap() -> None:
    assert _clip_ranges([(0.0, 1.0)], [(3.0, 4.0)]) == []

"""subtitles \u6a21\u5757\u5355\u5143\u6d4b\u8bd5\uff1aSRT/ASS \u89e3\u6790\u3001\u5199\u8bfb\u56de\u73af\u3001\u5185\u5d4c\u5b57\u5e55\u63d0\u53d6\u3002"""
from __future__ import annotations

from pathlib import Path

from app.subtitles import (
    extract_embedded_subtitles,
    parse_ass,
    parse_srt,
    parse_subtitle_file,
    write_srt,
)

SRT = """1
00:00:01,000 --> 00:00:03,500
\u3053\u3093\u306b\u3061\u306f \u4e16\u754c

2
00:00:05,000 --> 00:00:06,250
\u3055\u3088\u3046\u306a\u3089
"""


def test_parse_srt() -> None:
    subs = parse_srt(SRT)
    assert len(subs) == 2
    assert subs[0].start == 1.0
    assert subs[0].end == 3.5
    assert subs[0].text == "\u3053\u3093\u306b\u3061\u306f \u4e16\u754c"
    assert subs[1].text == "\u3055\u3088\u3046\u306a\u3089"


def test_parse_srt_ignores_bad_blocks() -> None:
    text = "garbage\n\n1\n00:00:01,000 --> 00:00:03,500\nok\n"
    subs = parse_srt(text)
    assert len(subs) == 1
    assert subs[0].text == "ok"


def test_parse_ass_strips_tags() -> None:
    ass = """[Script Info]
[Events]
Dialogue: 0,0:00:01.00,0:00:03.50,Default,,0,0,0,,{\\an8}Hello{\\i1} World{\\i0}
"""
    subs = parse_ass(ass)
    assert len(subs) == 1
    assert subs[0].start == 1.0
    assert subs[0].end == 3.5
    assert subs[0].text == "Hello World"


def test_write_read_roundtrip(tmp_path: Path) -> None:
    p = write_srt(tmp_path / "t.srt", [{"start": 1.0, "end": 2.5, "text": "\u3042\u3044\u3046"}])
    subs = parse_subtitle_file(p)
    assert len(subs) == 1
    assert subs[0].start == 1.0
    assert subs[0].end == 2.5
    assert subs[0].text == "\u3042\u3044\u3046"


def test_extract_embedded_none_when_no_subs(sample_video: Path, tmp_path: Path) -> None:
    out = extract_embedded_subtitles(sample_video, tmp_path / "no.srt")
    assert out is None

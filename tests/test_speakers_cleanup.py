"""孤儿角色清理 + 字幕生成片段（2026-09-25 说话人识别修复）。"""
from app.speakers import orphan_characters, segments_from_subs


def _ch(cid, labels, exp=None):
    c = {"id": cid, "name": "说话人", "speakerLabels": labels}
    if exp:
        c["exp"] = exp
    return c


LIVE = {"m-aaa", "m-bbb"}


def test_orphan_all_dead_labels_removed():
    chars = [_ch("c1", ["m-dead1:说话人1"]), _ch("c2", ["m-dead2:说话人2"])]
    out = orphan_characters(chars, LIVE)
    assert [c["id"] for c in out] == ["c1", "c2"]


def test_orphan_keeps_live_and_mixed():
    chars = [
        _ch("live", ["m-aaa:说话人1"]),
        _ch("mixed", ["m-aaa:说话人1", "m-dead:说话人3"]),  # 有活标签 -> 留
        _ch("nolabel", []),                                  # 手动建 -> 留
    ]
    assert orphan_characters(chars, LIVE) == []


def test_orphan_keeps_trained_and_referenced():
    chars = [
        _ch("trained", ["m-dead:说话人1"], exp="model.pth"),
        _ch("referenced", ["m-dead:说话人2"]),
    ]
    out = orphan_characters(chars, LIVE, referenced_char_ids={"referenced"})
    assert [c["id"] for c in out] == []


def test_segments_from_subs_shape():
    subs = [{"start": 1.5, "end": 3.0, "text": "はい"},
            {"start": 3.0, "end": 9.0, "text": "そうか"}]
    segs = segments_from_subs(subs)
    assert len(segs) == 2
    assert segs[0]["start"] == 1.5 and segs[0]["end"] == 3.0
    assert segs[0]["text"] == "はい"
    assert segs[0]["characterId"] is None and segs[0]["mixed"] is False
    assert segs[0]["id"] is None  # 由调用方统一分配


def test_segments_from_subs_drops_empty_and_accepts_objects():
    class S:
        start, end, text = 0.0, 2.0, "obj"

    segs = segments_from_subs([{"start": 5.0, "end": 5.0, "text": "零长"},
                               S()])
    assert len(segs) == 1 and segs[0]["text"] == "obj"

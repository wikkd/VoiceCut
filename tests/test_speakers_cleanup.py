"""孤儿角色清理 + 字幕生成片段（2026-09-25 说话人识别修复）。"""
from app.speakers import bind_segments, orphan_characters, segments_from_subs


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


# ── mixed 片段拆分（bind_segments new_id）───────────────────


def _counter():
    k = [0]

    def nid(prefix):
        k[0] += 1
        return f"{prefix}_{k[0]}"

    return nid


def test_bind_splits_mixed_segment():
    segs = [{"id": "s_1", "start": 10.0, "end": 16.0, "text": "ABCDE12345",
             "characterId": None, "speakerLabel": None, "mixed": False}]
    spk = [{"start": 10.0, "end": 13.0, "label": "A"},
           {"start": 13.0, "end": 16.0, "label": "B"}]
    out, mixed_n = bind_segments(segs, spk, {"A": "cA", "B": "cB"},
                                 new_id=_counter())
    assert mixed_n == 1
    assert len(out) == 2
    a, b = out
    assert (a["start"], a["end"], a["speakerLabel"], a["characterId"]) == \
        (10.0, 13.0, "A", "cA")
    assert (b["start"], b["end"], b["speakerLabel"], b["characterId"]) == \
        (13.0, 16.0, "B", "cB")
    assert a["mixed"] is False and b["mixed"] is False
    assert a["text"] + b["text"] == "ABCDE12345"  # 文本按时长比例切且不丢字
    assert a["id"] != b["id"]


def test_bind_without_new_id_keeps_old_behaviour():
    segs = [{"id": "s_1", "start": 10.0, "end": 16.0, "text": "x",
             "characterId": None, "speakerLabel": None, "mixed": False}]
    spk = [{"start": 10.0, "end": 13.0, "label": "A"},
           {"start": 13.0, "end": 16.0, "label": "B"}]
    out, mixed_n = bind_segments(segs, spk, {"A": "cA", "B": "cB"})
    assert mixed_n == 1 and len(out) == 1
    assert out[0]["mixed"] is True and out[0]["characterId"] is None


def test_bind_clean_segment_binds_normally():
    segs = [{"id": "s_1", "start": 0.0, "end": 4.0, "text": "はい",
             "characterId": None, "speakerLabel": None, "mixed": False}]
    spk = [{"start": 0.0, "end": 4.0, "label": "A"}]
    out, mixed_n = bind_segments(segs, spk, {"A": "cA"}, new_id=_counter())
    assert mixed_n == 0 and len(out) == 1
    assert out[0]["characterId"] == "cA" and out[0]["speakerLabel"] == "A"

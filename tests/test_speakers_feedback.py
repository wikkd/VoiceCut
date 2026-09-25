"""声纹反馈单元测试：locked 防覆盖 + 样本吸收 + 静默重匹配（monkeypatch 嵌入避免模型）。"""
from __future__ import annotations

import numpy as np
import pytest

from app import speakers


# ── locked：bind_segments 不重绑、不拆分 ──────────────────────

def test_bind_segments_respects_locked() -> None:
    spk = [{"start": 0.0, "end": 4.0, "label": "说话人1"}]
    char_of = {"说话人1": "char_A"}
    locked_seg = {"id": "s1", "start": 0.0, "end": 4.0, "text": "手改",
                  "characterId": "char_manual", "locked": True}
    normal_seg = {"id": "s2", "start": 0.0, "end": 4.0, "text": "自动"}
    out, mixed = speakers.bind_segments([locked_seg, normal_seg], spk, char_of)
    assert mixed == 0
    by_id = {s["id"]: s for s in out}
    assert by_id["s1"]["characterId"] == "char_manual"   # 锁定段角色不被覆盖
    assert by_id["s1"]["text"] == "手改"
    assert by_id["s2"]["characterId"] == "char_A"        # 普通段正常绑定


def test_bind_segments_locked_mixed_not_split() -> None:
    # 锁定段即使被判 mixed 也不拆分、不解绑
    spk = [{"start": 0.0, "end": 1.0, "label": "A"},
           {"start": 1.0, "end": 2.0, "label": "B"}]
    seg = {"id": "s1", "start": 0.0, "end": 2.0, "text": "人工成果",
           "characterId": "char_X", "locked": True}
    out, mixed = speakers.bind_segments([seg], spk, {}, new_id=lambda p: f"{p}_n")
    assert len(out) == 1 and out[0]["characterId"] == "char_X"
    assert mixed == 0   # 未计入待拆分


# ── locked：normalize_segments 不删除/不吞噬 ──────────────────

def test_normalize_segments_protects_locked() -> None:
    dup_a = {"id": "a", "start": 1.0, "end": 5.0, "text": "长", "locked": True}
    dup_b = {"id": "b", "start": 1.0, "end": 4.0, "text": "短"}   # 与 a 同起点、被包含
    out, removed = speakers.normalize_segments([dup_a, dup_b])
    assert removed == 0                          # 锁定段不被删，也不吞噬 b
    assert {s["id"] for s in out} == {"a", "b"}
    # 对照：无锁定时照常去重
    out2, removed2 = speakers.normalize_segments(
        [{**dup_a, "locked": False}, dup_b])
    assert removed2 == 1 and {s["id"] for s in out2} == {"a"}


# ── absorb_character_sample：质心运行平均 ─────────────────────

def test_absorb_character_sample() -> None:
    base = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    chars = [{"id": "c1", "embedding": speakers.embedding_to_b64(base), "emb_count": 1}]
    sample = np.array([0.0, 1.0, 0.0])
    out = speakers.absorb_character_sample(chars, "c1", sample)
    emb = speakers.embedding_from_b64(out[0]["embedding"])
    expected = (base + sample) / 2
    np.testing.assert_allclose(emb, expected, atol=1e-6)
    assert out[0]["emb_count"] == 2
    # 角色不存在 / 样本无效 → 原样返回
    assert speakers.absorb_character_sample(chars, "nope", sample) == chars
    assert speakers.absorb_character_sample(chars, "c1", None) == chars


# ── rescan_assignments：静默重匹配规则 ────────────────────────

@pytest.fixture()
def fake_embed(monkeypatch, tmp_path):
    """按片段中点返回两簇嵌入：中点<5 → 簇A[1,0]，否则簇B[0,1]。"""
    def _pick():
        return _fake
    def _fake(mono, sr, start, end):
        return np.array([1.0, 0.0]) if (start + end) / 2 < 5 else np.array([0.0, 1.0])
    monkeypatch.setattr(speakers, "_pick_embed_fn", _pick)
    monkeypatch.setattr(speakers, "read_mono16k",
                        lambda p: (np.zeros(16000, dtype=np.float32), 16000))
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"RIFF")   # 只需存在（read_mono16k 已被 mock）
    return _fake, str(wav)


def test_rescan_binds_unassigned_and_skips_locked(fake_embed) -> None:
    _, wav = fake_embed
    chars = [{"id": "cA", "embedding": speakers.embedding_to_b64(np.array([1.0, 0.0])), "emb_count": 3},
             {"id": "cB", "embedding": speakers.embedding_to_b64(np.array([0.0, 1.0])), "emb_count": 3}]
    segs = [
        {"id": "u1", "start": 2.0, "end": 3.0, "characterId": None},            # → cA
        {"id": "lk", "start": 2.5, "end": 3.5, "characterId": None, "locked": True},  # 跳过
        {"id": "mx", "start": 3.0, "end": 4.0, "characterId": None, "mixed": True},   # 跳过
        {"id": "u2", "start": 8.0, "end": 9.0, "characterId": None},            # → cB
    ]
    sources = [{"item_id": "it1", "wav_path": wav, "segments": segs}]
    changes, stats = speakers.rescan_assignments(sources, chars)
    assert changes == {("it1", "u1"): "cA", ("it1", "u2"): "cB"}
    assert stats["bound"] == 2 and stats["skipped"] == 2


def test_rescan_moves_only_with_margin(fake_embed) -> None:
    _, wav = fake_embed
    chars = [{"id": "cA", "embedding": speakers.embedding_to_b64(np.array([1.0, 0.0])), "emb_count": 5},
             {"id": "cB", "embedding": speakers.embedding_to_b64(np.array([0.0, 1.0])), "emb_count": 5}]
    # 段中点 7（簇B），当前绑 cA → 应以明显差距改绑 cB
    segs = [{"id": "m1", "start": 6.0, "end": 8.0, "characterId": "cA"},
            {"id": "m2", "start": 6.0, "end": 8.0, "characterId": "cB"}]  # 已正确 → 不动
    sources = [{"item_id": "it1", "wav_path": wav, "segments": segs}]
    changes, stats = speakers.rescan_assignments(sources, chars)
    assert changes == {("it1", "m1"): "cB"}
    assert stats["moved"] == 1 and stats["bound"] == 0

"""识别 / 声纹反馈的整段写回不得覆盖人工说话人指派。

用户报告：「**在我修改说话人后，进化识别系统有概率把我设定好的说话人再修改**」。

两条**独立**泄露路径：

① **后端（本文件主测）** —— `_speakers_worker` / `_project_speakers_run` /
   `_speakers_feedback_worker` 都是「load_project →（秒~分钟）→ normalize_segments
   + bind_segments → save_project」的**整段覆盖写回**，而 normalize/bind 判定
   `locked` 读的是 **load 那一刻的快照**。用户改完说话人只经前端 400ms 防抖 POST
   落盘，这次 POST 落在 load…save 之间就被聚类结果覆盖 —— 实测窗口 ≈ 39 ms/素材
   （`scripts/diag_identify_race_window.py`），全项目 18 素材 ≈ 0.24 次/轮，故「有概率」。
   另有 4 处孤儿/陈旧角色清理循环只查 `characterId`、**完全没查 `locked`**。

② **前端** —— 识别任务结束时 `loadAllItemData()` 用服务端状态**无条件**覆盖内存，
   抹掉还在防抖队列里（或保存请求被 `saveInFlight` 静默丢弃）的本地改动；随后
   任意一次保存会把这份服务端旧版本当"用户数据"写回，**永久固化**。
   由 `scripts/browser_test.js` 的 SPK 段覆盖。

修复（对应 ①）：`app/project.py` 的 `merge_locked_segments` +
`save_project_guarding_locked`（在 db 写锁内「重读磁盘 → 合并 locked → 写入」，
把窗口压到 0）、`app/web/projects.py` 的 `_save_writeback` 统一出口与
`_detach_character_refs` 的 locked 保护。
"""
from __future__ import annotations

from pathlib import Path

from app import db
from app import project as pr
from app import speakers as sp
from app.config import AppConfig
from app.media_store import MediaItem, MediaStore
from app.tasks import TaskManager
from app.web import projects as projects_bp
from app.web.context import WebContext


def _mkctx(tmp_path: Path):
    cfg = AppConfig(workdir=tmp_path)
    store = MediaStore(cfg.workdir)
    tm = TaskManager(gpu_workers=1, cpu_workers=1)
    return cfg, WebContext(cfg, store, tm, None)


def _user_version() -> dict:
    """用户在 worker 运行期间改好并落盘的那一段（人工改文本 + 改说话人 + 锁定）。"""
    return {"id": "s1", "start": 0.0, "end": 4.0, "text": "人工改过",
            "characterId": "cUSER", "locked": True}


def _stale_snapshot() -> dict:
    """worker 手里 load 时的旧快照：还没有 locked，说话人未指派。"""
    return {"segments": [{"id": "s1", "start": 0.0, "end": 4.0, "text": "自动",
                          "characterId": None}],
            "speaker_segments": [{"start": 0.0, "end": 4.0, "label": "S1"}]}


def _run_worker_writeback(stale: dict) -> dict:
    """复刻 worker 的 load 之后半段：normalize → bind → 返回待写数据。"""
    segs, _ = sp.normalize_segments(stale["segments"])
    segs, _ = sp.bind_segments(segs, stale["speaker_segments"], {"S1": "cAUTO"},
                               new_id=lambda p: f"{p}_n")
    return {**stale, "segments": segs}


# ── 判据标定：先证明"覆盖确实会发生" ───────────────────────────

def test_plain_save_project_clobbers_manual_assignment(tmp_path: Path) -> None:
    """反向对照：普通 save_project 是整段覆盖 → 人工指派被聚类结果冲掉。

    固定住 bug 形态，使下面的守护版用例不可能靠"判据失灵"假绿。
    """
    stale = _run_worker_writeback(_stale_snapshot())
    # 用户在 worker 的 load 之后落盘
    pr.save_project(tmp_path, "it1", {"segments": [_user_version()]})

    pr.save_project(tmp_path, "it1", stale)          # worker 写回（旧通道）

    got = pr.load_project(tmp_path, "it1")["segments"][0]
    assert got["characterId"] == "cAUTO"             # 人工指派被覆盖 ← bug
    assert got["text"] == "自动"
    assert not got.get("locked")


def test_guarding_save_keeps_manual_assignment(tmp_path: Path) -> None:
    """守护版写回：同一时序下人工指派 / 人工文本 / locked 全部保留。"""
    stale = _run_worker_writeback(_stale_snapshot())
    pr.save_project(tmp_path, "it1", {"segments": [_user_version()]})

    pr.save_project_guarding_locked(tmp_path, "it1", stale)

    got = pr.load_project(tmp_path, "it1")["segments"][0]
    assert got["characterId"] == "cUSER"
    assert got["text"] == "人工改过"
    assert got["locked"] is True


def test_guarding_save_is_noop_without_locked(tmp_path: Path) -> None:
    """无 locked 时守护版与普通版逐字节等价（不能把自动识别结果卡住）。"""
    stale = _run_worker_writeback(_stale_snapshot())
    pr.save_project(tmp_path, "it1", {"segments": [
        {"id": "s1", "start": 0.0, "end": 4.0, "text": "自动", "characterId": None}]})

    pr.save_project_guarding_locked(tmp_path, "it1", stale)

    got = pr.load_project(tmp_path, "it1")["segments"][0]
    assert got["characterId"] == "cAUTO"             # 正常自动绑定照旧生效
    assert got["speakerLabel"] == "S1"


# ── merge_locked_segments 单元 ────────────────────────────────

def test_merge_locked_prefers_disk_version() -> None:
    prev = [_user_version()]
    incoming = [{"id": "s1", "start": 0.0, "end": 4.0, "text": "自动",
                 "characterId": "cAUTO", "speakerLabel": "S1", "mixed": True}]
    out = pr.merge_locked_segments(prev, incoming)
    assert len(out) == 1
    assert out[0]["characterId"] == "cUSER"
    assert out[0]["text"] == "人工改过"
    assert out[0]["locked"] is True
    assert "mixed" not in out[0]                     # 人工版本说了算，聚类标记不残留


def test_merge_locked_restores_swallowed_segment() -> None:
    """locked 段被去重/拆 mixed/字幕重建吞掉时：补回人工版本，并移除其非锁定替身。"""
    prev = [{"id": "s1", "start": 0.0, "end": 4.0, "text": "人工",
             "characterId": "cU", "locked": True}]
    incoming = [{"id": "s1a", "start": 0.0, "end": 2.0, "text": "人", "characterId": "cA"},
                {"id": "s1b", "start": 2.0, "end": 4.0, "text": "工", "characterId": "cA"},
                {"id": "s9", "start": 10.0, "end": 12.0, "text": "别的", "characterId": "cA"}]
    out = pr.merge_locked_segments(prev, incoming)
    ids = [s["id"] for s in out]
    assert "s1" in ids and "s1a" not in ids and "s1b" not in ids
    assert "s9" in ids                               # 不相干的片段不动
    assert next(s for s in out if s["id"] == "s1")["characterId"] == "cU"


def test_merge_locked_noop_without_locked() -> None:
    """无 locked 片段 → 原样返回，不复制、不排序（零副作用）。"""
    incoming = [{"id": "a", "start": 0.0, "end": 1.0}]
    prev = [{"id": "a", "start": 0.0, "end": 1.0},
            {"id": "b", "start": 1.0, "end": 2.0, "characterId": "cX"}]
    out = pr.merge_locked_segments(prev, incoming)
    assert out == incoming and out[0] is incoming[0]
    # 空输入也不炸
    assert pr.merge_locked_segments([], []) == []
    assert pr.merge_locked_segments(None, None) == []


# ── 孤儿 / 陈旧角色清理不得解绑人工锁定段 ──────────────────────

def test_detach_character_refs_skips_locked() -> None:
    segs = [
        {"id": "lk", "characterId": "cOLD", "speakerLabel": "S1", "locked": True},
        {"id": "au", "characterId": "cOLD", "speakerLabel": "S1"},
        {"id": "ok", "characterId": "cKEEP", "speakerLabel": "S2"},
    ]
    changed = projects_bp._detach_character_refs(segs, {"cOLD"}, drop_label=True)
    assert changed is True
    assert segs[0]["characterId"] == "cOLD"          # 人工锁定段保留（含 label）
    assert segs[0]["speakerLabel"] == "S1"
    assert segs[1]["characterId"] is None            # 自动段照旧解绑
    assert segs[1]["speakerLabel"] is None
    assert segs[2]["characterId"] == "cKEEP"
    # 只有 locked 引用目标 → 无改动（不会产生一次空写回）
    assert projects_bp._detach_character_refs(
        [{"id": "lk", "characterId": "cOLD", "locked": True}], {"cOLD"}) is False


# ── 端到端：真实 _project_speakers_run 的 load…save 窗口 ────────

def test_project_identify_keeps_manual_speaker_under_race(tmp_path: Path,
                                                          monkeypatch) -> None:
    """确定性复现原本"有概率"的时间差，断言识别后人工说话人指派仍在。

    注入点在 ``normalize_segments``（worker 已 load、尚未 save 的窗口正中）：
    该函数被调用时让"用户"改好说话人并落盘。同时断言识别**确实**为标签 S1 建了
    自动角色，证明用例不是靠"识别什么都没干"骗过断言。
    """
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")                     # 只需存在（不真的解码）
    c.store.add(MediaItem(id="it1", name="a", wav_path=wav, duration=4.0,
                          sample_rate=48000, project_id=pid))
    monkeypatch.setattr(c, "load_subs",
                        lambda item: [{"start": 0.0, "end": 4.0, "text": "hi"}])
    pr.save_project(cfg.workdir, "it1", {"segments": [
        {"id": "s1", "start": 0.0, "end": 4.0, "text": "自动", "characterId": None}]})

    # quality="mfcc" → 走"一标签一角色"分支，避开真实声纹模型；
    # label_embeddings 非空才会为 S1 建角色（否则 char_of_label 为空、绑不上任何人）
    def fake_generate(sources, progress_cb=None):
        return {"items": [{"speaker_segments": [{"start": 0.0, "end": 4.0, "label": "S1"}],
                           "labeled": 1, "mixed": 0, "total": 1}],
                "label_embeddings": {"S1": [0.0, 1.0]},
                "n_speakers": 1, "quality": "mfcc"}

    monkeypatch.setattr(projects_bp.speakers_mod,
                        "generate_speakers_project", fake_generate)

    real_norm = projects_bp.speakers_mod.normalize_segments
    landed: dict = {}

    def norm_hook(segments, **kw):
        # ← worker 的 load…save 窗口正中：用户改好说话人 + 锁定，防抖 POST 落盘
        pr.save_project(cfg.workdir, "it1", {"segments": [_user_version()]})
        landed["yes"] = True
        return real_norm(segments, **kw)

    monkeypatch.setattr(projects_bp.speakers_mod, "normalize_segments", norm_hook)

    out = projects_bp._project_speakers_run(c, pid)

    assert landed.get("yes") is True                    # 注入点确实命中
    # 非平凡：识别**确实**为标签 S1 建了自动角色（否则"人工指派保留"可能只是
    # 因为 bind 根本没能指派任何人，用例就失去区分力）
    assert any((ch.get("speakerLabels") or []) and ch["speakerLabels"][0].endswith(":S1")
               for ch in out["characters"]), out["characters"]
    assert out["created"], out
    seg = pr.load_project(cfg.workdir, "it1")["segments"][0]
    assert seg["characterId"] == "cUSER"
    assert seg["text"] == "人工改过"
    assert seg["locked"] is True

"""重新识别（reset）测试：清空角色池与片段指派 + reset 参数透传识别 worker。"""
from __future__ import annotations

import time
from pathlib import Path

from app import db
from app import project as pr
from app.config import AppConfig
from app.media_store import MediaStore
from app.tasks import TaskManager
from app.web import projects as projects_bp
from app.web.context import WebContext


def _mkctx(tmp_path: Path):
    cfg = AppConfig(workdir=tmp_path)
    store = MediaStore(cfg.workdir)
    tm = TaskManager(gpu_workers=1, cpu_workers=1)
    return cfg, WebContext(cfg, store, tm, None)


def _wait_done(tm: TaskManager, tid: str) -> None:
    for _ in range(200):
        t = tm.get(tid)
        if t and t["status"] in ("done", "error", "cancelled"):
            return
        time.sleep(0.02)


# ── _reset_project_pool：清空池 + 解除指派，保留文本与 locked ──

def test_reset_project_pool_clears_pool_and_assignments(tmp_path: Path) -> None:
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    pr.save_pool(cfg.workdir, pid, [
        {"id": "c1", "name": "甲", "speakerLabels": ["p1:S0"]},
        {"id": "c2", "name": "乙", "speakerLabels": []},
    ])
    pr.save_project(cfg.workdir, "it1", {"segments": [
        {"id": "s1", "start": 0.0, "end": 1.0, "text": "人工改过",
         "characterId": "c1", "speakerLabel": "SPEAKER_00", "locked": True},
        {"id": "s2", "start": 1.0, "end": 2.0, "text": "自动", "characterId": "c2"},
        {"id": "s3", "start": 2.0, "end": 3.0, "text": "未分配"},
    ]})
    pr.save_project(cfg.workdir, "it2", {"segments": []})

    n = projects_bp._reset_project_pool(c, pid, ["it1", "it2"])
    assert n == 2                                   # 清掉 2 个角色
    assert pr.load_pool(cfg.workdir, pid)["characters"] == []
    segs = pr.load_project(cfg.workdir, "it1")["segments"]
    by_id = {s["id"]: s for s in segs}
    # 指派解除
    assert by_id["s1"]["characterId"] is None
    assert by_id["s1"]["speakerLabel"] is None
    assert by_id["s2"]["characterId"] is None
    # 人工成果保留：文本 + locked 标记
    assert by_id["s1"]["text"] == "人工改过"
    assert by_id["s1"]["locked"] is True
    # 原本无指派的段不动
    assert by_id["s3"].get("characterId") is None
    # 空片段素材不报错
    assert pr.load_project(cfg.workdir, "it2")["segments"] == []


def test_reset_with_empty_pool_returns_zero(tmp_path: Path) -> None:
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    pr.save_project(cfg.workdir, "it1", {"segments": [
        {"id": "s1", "start": 0.0, "end": 1.0, "text": "a", "characterId": "ghost"}]})
    n = projects_bp._reset_project_pool(c, pid, ["it1"])
    assert n == 0
    # 悬空指派仍被解除（角色不存在也一样清）
    seg = pr.load_project(cfg.workdir, "it1")["segments"][0]
    assert seg["characterId"] is None


# ── reset 参数透传识别 worker ────────────────────────────────

def test_submit_analyze_threads_reset(tmp_path: Path, monkeypatch) -> None:
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    seen: dict = {}

    def fake_run(ctx, project_id: str, reset: bool = False) -> dict:
        seen["reset"] = reset
        return {"ok": True, "characters": []}

    monkeypatch.setattr(projects_bp, "_project_speakers_run", fake_run)
    tid = projects_bp.submit_project_analyze(c, pid, force=True, reset=True)
    _wait_done(c.tasks, tid)
    assert seen["reset"] is True

    # 不带 reset → 默认 False
    tid2 = projects_bp.submit_project_analyze(c, pid, force=True)
    _wait_done(c.tasks, tid2)
    assert seen["reset"] is False

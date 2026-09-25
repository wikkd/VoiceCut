"""后台自动分析（导入后自动生成字幕 + 说话人识别）相关测试。"""
from __future__ import annotations

import threading
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


# ── 项目设置 ─────────────────────────────────────────────
def test_project_settings_roundtrip(tmp_path: Path) -> None:
    pid = db.ensure_default_project(db.get_conn(tmp_path))
    # 默认开启
    assert pr.get_project_setting(tmp_path, pid, "auto_analyze", True) is True
    pr.set_project_setting(tmp_path, pid, "auto_analyze", False)
    assert pr.get_project_setting(tmp_path, pid, "auto_analyze", True) is False
    # 设置不应破坏角色池
    pr.save_pool(tmp_path, pid, [{"id": "c1", "name": "A", "speakerLabels": []}])
    assert pr.load_pool(tmp_path, pid)["characters"][0]["name"] == "A"
    assert pr.get_project_setting(tmp_path, pid, "auto_analyze", True) is False


def test_submit_project_analyze_obey_setting(tmp_path: Path) -> None:
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    pr.set_project_setting(cfg.workdir, pid, "auto_analyze", False)
    assert projects_bp.submit_project_analyze(c, pid) is None  # 关闭时返回 None
    # force=True 不受设置影响
    tid = projects_bp.submit_project_analyze(c, pid, force=True)
    assert tid is not None
    _wait_done(c.tasks, tid)


def test_submit_project_analyze_dedup_and_rerun(tmp_path: Path) -> None:
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))

    release = threading.Event()
    calls: list[str] = []

    def fake_run(ctx, project_id: str, reset: bool = False) -> dict:
        calls.append(project_id)
        release.wait(timeout=5)
        return {"ok": True, "characters": []}

    original = projects_bp._project_speakers_run
    projects_bp._project_speakers_run = fake_run
    try:
        tid1 = projects_bp.submit_project_analyze(c, pid, force=True)
        time.sleep(0.1)  # 让第一个任务进入 running
        tid2 = projects_bp.submit_project_analyze(c, pid, force=True)  # 去重 → 复用 tid1
        assert tid2 == tid1
        assert c.auto_analyze_pending.get(pid) is True  # 打上“重跑”标记
        release.set()
        _wait_done(c.tasks, tid1)
        # 完成后自动再排一轮，最终跑了两轮
        for _ in range(200):
            if len(calls) >= 2 and not c.auto_analyze_tasks.get(pid):
                break
            time.sleep(0.02)
        assert len(calls) == 2
        assert c.auto_analyze_tasks.get(pid) is None
    finally:
        projects_bp._project_speakers_run = original
        release.set()

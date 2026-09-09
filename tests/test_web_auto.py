"""项目设置 / 活跃任务 / 导入自动分析链 的 Flask 路由测试。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from app.config import AppConfig
from app.web import create_app


def test_web_project_settings_and_active_tasks(tmp_path: Path) -> None:
    app = create_app(AppConfig(workdir=tmp_path))
    client = app.test_client()

    r = client.post("/api/projects", json={"name": "P"})
    pid = r.get_json()["id"]

    # 默认 auto_analyze 开启
    assert client.get(f"/api/projects/{pid}").get_json()["auto_analyze"] is True

    # 关闭并读回
    r = client.post(f"/api/projects/{pid}/settings", json={"auto_analyze": False})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}").get_json()["auto_analyze"] is False
    r = client.post(f"/api/projects/{pid}/settings", json={"auto_analyze": True})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{pid}").get_json()["auto_analyze"] is True

    # 活跃任务：带 kind 的慢任务出现在 /api/tasks/active
    tm = app.extensions["vc_tasks"]
    hold = threading.Event()

    def slow() -> int:
        hold.wait(timeout=5)
        return 1

    tid = tm.submit(slow, kind="import")
    time.sleep(0.05)
    tasks = client.get("/api/tasks/active").get_json()
    assert any(t["id"] == tid and t["kind"] == "import" for t in tasks)

    # 任务记录保留 kind
    t = client.get(f"/api/tasks/{tid}").get_json()
    assert t["kind"] == "import"
    hold.set()
    time.sleep(0.05)
    assert client.get("/api/tasks/active").get_json() == []


def test_import_chains_auto_analyze(tmp_path: Path, sample_video: Path, monkeypatch) -> None:
    """导入任务完成后自动提交项目级分析，并把 auto_task_id 带回给前端。"""
    from app.web import projects as projects_bp

    app = create_app(AppConfig(workdir=tmp_path))
    client = app.test_client()
    r = client.post("/api/projects", json={"name": "P"})
    pid = r.get_json()["id"]

    calls: list[str] = []

    def fake_submit(c, project_id: str, *, force: bool = False) -> str:
        calls.append(project_id)
        return "auto-fake-1"

    monkeypatch.setattr(projects_bp, "submit_project_analyze", fake_submit)

    with open(sample_video, "rb") as fh:
        r = client.post(
            "/api/import",
            data={"file": (fh, "test.mp4"), "project_id": pid},
            content_type="multipart/form-data",
        )
    assert r.status_code == 200
    tid = r.get_json()["task_id"]

    t = None
    for _ in range(300):
        t = client.get(f"/api/tasks/{tid}").get_json()
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert t is not None and t["status"] == "done"
    assert calls == [pid]  # 自动分析以项目 id 提交
    assert t["result"]["auto_task_id"] == "auto-fake-1"

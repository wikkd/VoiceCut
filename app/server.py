"""Flask 后端：静态页 + REST API。

当前阶段（01/02）只提供健康检查与配置查询；
导入/波形/选区/播放/导出/降噪/分离/转写/数据集/B站代理端点随后续阶段加入。
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, send_from_directory

from app.config import AppConfig
from app.media_store import MediaStore
from app.tasks import TaskManager

STATIC_DIR = Path(__file__).parent / "static"


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig()
    app = Flask(__name__, static_folder=None)
    app.config["VC_CFG"] = cfg

    store = MediaStore()
    tasks = TaskManager()
    app.extensions["vc_store"] = store
    app.extensions["vc_tasks"] = tasks

    # ── 静态页面 ────────────────────────────────────────────────
    @app.get("/")
    def index() -> object:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str) -> object:
        return send_from_directory(STATIC_DIR, filename)

    # ── 基础 API ────────────────────────────────────────────────
    @app.get("/api/health")
    def health() -> object:
        return jsonify({"ok": True, "version": "0.1.0", "stage": "02-env-verified"})

    @app.get("/api/config")
    def api_config() -> object:
        return jsonify({
            "ffmpeg": cfg.ffmpeg_path,
            "sample_rates": list(cfg.sample_rates),
            "dataset_sample_rate": cfg.dataset_sample_rate,
            "workdir": str(cfg.workdir),
        })

    @app.get("/api/tasks")
    def list_tasks() -> object:
        return jsonify(tasks.all())

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> object:
        t = tasks.get(task_id)
        return jsonify(t) if t else (jsonify({"error": "not found"}), 404)

    @app.get("/api/items")
    def list_items() -> object:
        items = [{
            "id": i.id, "name": i.name, "kind": i.kind,
            "duration": i.duration, "sample_rate": i.sample_rate,
            "derived_from": i.derived_from,
        } for i in store.all()]
        return jsonify(items)

    return app

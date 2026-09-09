"""Flask web 层：create_app 组装 + Blueprint 注册 + 静态页/健康检查。

原 app/server.py 的单一闭包被拆为 WebContext（app.web.context）与多个
Blueprint（media/projects/subtitles/training/tasks/bilibili），路由路径与
行为保持一致。
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, send_from_directory

from app.config import AppConfig
from app.log import get_logger
from app.media_store import MediaStore
from app.tasks import TaskManager
from app.web import bilibili as bilibili_bp
from app.web import media as media_bp
from app.web import projects as projects_bp
from app.web import subtitles as subtitles_bp
from app.web import tasks as tasks_bp
from app.web import training as training_bp
from app.web.context import WebContext

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig()
    app = Flask(__name__, static_folder=None)
    app.config["VC_CFG"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4GB 上传上限

    log = get_logger()
    store = MediaStore(cfg.workdir)
    tasks = TaskManager()
    ctx = WebContext(cfg, store, tasks, log)
    app.extensions["vc_store"] = store
    app.extensions["vc_tasks"] = tasks
    app.extensions["vc_ctx"] = ctx

    # ── 静态页面 / 基础配置 ──────────────────────────────────

    @app.get("/")
    def index() -> object:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str) -> object:
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/health")
    def health() -> object:
        return jsonify({"ok": True, "version": "0.1.0", "stage": "06-complete"})

    @app.get("/api/config")
    def api_config() -> object:
        return jsonify({
            "ffmpeg": cfg.ffmpeg_path,
            "sample_rates": list(cfg.sample_rates),
            "dataset_sample_rate": cfg.dataset_sample_rate,
            "workdir": str(cfg.workdir),
            "models": {"whisper": ["medium", "large-v3"], "demucs": "htdemucs"},
        })

    # ── Blueprint 注册 ───────────────────────────────────────
    app.register_blueprint(media_bp.bp)
    app.register_blueprint(projects_bp.bp)
    app.register_blueprint(subtitles_bp.bp)
    app.register_blueprint(training_bp.bp)
    app.register_blueprint(tasks_bp.bp)
    app.register_blueprint(bilibili_bp.bp)
    return app

"""后台任务路由（Blueprint）。"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify

bp = Blueprint("tasks", __name__)


def ctx():
    return current_app.extensions["vc_ctx"]


@bp.get("/api/tasks")
def list_tasks() -> object:
    return jsonify(ctx().tasks.all())


@bp.get("/api/tasks/active")
def active_tasks() -> object:
    """运行中/排队中的任务（前端刷新后可重新挂接进度与完成回调）。"""
    return jsonify([t for t in ctx().tasks.all()
                    if t["status"] in ("pending", "running")])


@bp.get("/api/tasks/<task_id>")
def get_task(task_id: str) -> object:
    t = ctx().tasks.get(task_id)
    return jsonify(t) if t else (jsonify({"error": "not found"}), 404)


@bp.post("/api/tasks/<task_id>/cancel")
def cancel_task(task_id: str) -> object:
    ok = ctx().tasks.cancel(task_id)
    return jsonify({"ok": ok})

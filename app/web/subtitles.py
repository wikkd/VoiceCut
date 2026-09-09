"""字幕路由（Blueprint）：SRT/ASS 上传、内嵌提取、Whisper 生成。"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app import subtitles as subtitles_mod
from app import transcribe as transcribe_mod
from app.media_store import MediaItem

bp = Blueprint("subtitles", __name__)


def ctx():
    return current_app.extensions["vc_ctx"]


@bp.get("/api/subtitles/<item_id>")
def api_subtitles(item_id: str) -> object:
    c = ctx()
    item = c.store.require(item_id)
    return jsonify({"id": item.id, "subs": c.load_subs(item)})


@bp.post("/api/subtitles/<item_id>")
def api_subtitles_upload(item_id: str) -> object:
    c = ctx()
    item = c.store.require(item_id)
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "缺少字幕文件"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".srt", ".ass", ".ssa"):
        return jsonify({"error": "仅支持 .srt / .ass / .ssa 字幕"}), 400
    subs_dir = c.cfg.workdir / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)
    path = subs_dir / f"{item.id}{ext}"
    f.save(str(path))
    subs = subtitles_mod.parse_subtitle_file(path)
    if not subs:
        return jsonify({"error": "字幕为空或无法解析"}), 400
    item.extra["subs_file"] = str(path)
    c.store.persist(item)
    return jsonify({"count": len(subs), "subs": [s.to_dict() for s in subs]})


@bp.post("/api/subtitles/<item_id>/generate")
def api_subtitles_generate(item_id: str) -> object:
    c = ctx()
    item = c.store.require(item_id)
    body = request.get_json(force=True) or {}
    model = (body.get("model") or "medium").lower()
    if model not in ("tiny", "base", "small", "medium", "large-v3"):
        return jsonify({"error": "未知模型"}), 400
    tid = c.tasks.submit(_subs_generate_worker, c, item, model, gpu=True)
    return jsonify({"task_id": tid})


def _subs_generate_worker(c, item: MediaItem, model: str) -> dict:
    tid = c.tasks.current_task_id()
    subs = transcribe_mod.transcribe_timed(
        item.wav_path, language="ja", model=model,
        progress_cb=lambda p: c.tasks.update(tid, progress=p,
                                             message=f"识别中 {p*100:.0f}%"),
    )
    if subs:
        subs_dir = c.cfg.workdir / "subs"
        subs_dir.mkdir(parents=True, exist_ok=True)
        path = subtitles_mod.write_srt(subs_dir / f"{item.id}.srt", subs)
        item.extra["subs_file"] = str(path)
        c.store.persist(item)
    return {"count": len(subs), "subs": subs, "kept_existing": not subs}

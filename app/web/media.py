"""素材 / 导入 / 流式 / 清洗 / 转写 / 训练集导出 路由（Blueprint）。"""
from __future__ import annotations

import concurrent.futures
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app import dataset as dataset_mod
from app import denoise as denoise_mod
from app import project as project_mod
from app import separate as separate_mod
from app import subtitles as subtitles_mod
from app import transcribe as transcribe_mod
from app.audio_ops import compute_peaks
from app.ffmpeg_util import export_segment, extract_audio, remux_preview, trim_silence
from app.media_store import MediaItem
from app.streaming import stream_file

bp = Blueprint("media", __name__)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".aac", ".ogg", ".m4a", ".opus"}


def ctx():
    return current_app.extensions["vc_ctx"]


@bp.get("/api/items")
def list_items() -> object:
    c = ctx()
    project_id = request.args.get("project_id") or None
    items = c.store.by_project(project_id) if project_id else c.store.all()
    return jsonify([c.item_json(i) for i in items])


@bp.delete("/api/items/<item_id>")
def delete_item(item_id: str) -> object:
    c = ctx()
    item = c.store.get(item_id)
    c.store.remove(item_id)
    if item is not None:
        _delete_item_files(c, item)
    return jsonify({"ok": True})


def _delete_item_files(c, item: MediaItem) -> None:
    """尽力删除素材工作文件（wav/预览/字幕/来源副本），失败静默。"""
    paths = {p for p in (item.wav_path, item.preview_mp4) if p}
    f = item.extra.get("subs_file")
    if f:
        paths.add(Path(f))
    src = Path(item.source) if item.source else None
    wd = Path(c.cfg.workdir).resolve()
    if (src and src not in paths and src.exists()
            and src.resolve().is_relative_to(wd)):
        paths.add(src)
    for p in paths:
        try:
            if p and p.exists():
                p.unlink()
        except OSError:
            pass
    project_mod.delete_project(c.cfg.workdir, item.id)


@bp.post("/api/items/<item_id>/rename")
def api_item_rename(item_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "名称为空"}), 400
    item = c.store.require(item_id)
    item.name = name
    c.store.persist(item)
    return jsonify(c.item_json(item))


# ── 导入 ─────────────────────────────────────────────────

@bp.post("/api/import")
def api_import() -> object:
    c = ctx()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "缺少文件"}), 400
    filename = Path(f.filename).name
    ext = Path(filename).suffix.lower()
    upload_dir = c.cfg.workdir / "imports"
    upload_dir.mkdir(parents=True, exist_ok=True)
    raw_path = upload_dir / (uuid.uuid4().hex + ext)
    f.save(str(raw_path))
    project_id = request.form.get("project_id") or ""
    tid = c.tasks.submit(_import_worker, c, raw_path, filename, project_id, kind="import")
    return jsonify({"task_id": tid, "name": filename})


def _import_worker(c, raw_path: Path, filename: str, project_id: str = "") -> dict:
    raw_path = Path(raw_path)
    stem = Path(filename).stem or "voicecut"
    ext = raw_path.suffix.lower()
    kind = "video" if ext in VIDEO_EXTS else "audio"
    items_dir = c.cfg.workdir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)

    item_id = c.store.new_id()
    wav = items_dir / f"{item_id}.wav"
    # 视频导入时 抽音频 / 转预览 / 提内嵌字幕 三路并行（只读同一源文件，互不干扰）
    preview = None
    subs_file = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_extract = ex.submit(extract_audio, raw_path, wav,
                              sample_rate=48000, channels=1)
        f_preview = f_subs = None
        if kind == "video":
            f_preview = ex.submit(remux_preview, raw_path,
                                  items_dir / f"{item_id}.preview.mp4")
            f_subs = ex.submit(subtitles_mod.extract_embedded_subtitles,
                               raw_path, items_dir / f"{item_id}.srt")
        f_extract.result()  # 抽音频失败则整体失败（保持原行为）
        if f_preview is not None:
            try:
                preview = f_preview.result()
            except Exception:  # noqa: BLE001
                preview = None
        if f_subs is not None:
            try:
                subs_file = f_subs.result()
            except Exception:  # noqa: BLE001
                subs_file = None
    item = c.register_item(item_id=item_id, wav=wav, preview=preview, name=stem,
                           kind=kind, source=str(raw_path),
                           project_id=project_id or None)
    if subs_file:
        item.extra["subs_file"] = str(subs_file)
        c.store.persist(item)
    result = {"item_id": item.id, "item": c.item_json(item)}
    auto_tid = _auto_analyze_after_import(c, item.project_id or "")
    if auto_tid:
        result["auto_task_id"] = auto_tid
    return result


def _auto_analyze_after_import(c, project_id: str) -> str | None:
    """导入完成后自动触发项目级分析（缺字幕先 Whisper 生成，再做说话人识别）。"""
    from app.web.projects import submit_project_analyze  # 延迟导入避免 Blueprint 循环依赖
    return submit_project_analyze(c, project_id)


# ── 流式 / 波形 ───────────────────────────────────────────

@bp.get("/api/audio/<item_id>")
def api_audio(item_id: str) -> object:
    item = ctx().store.require(item_id)
    if not item.wav_path.exists():
        return jsonify({"error": "missing"}), 404
    return stream_file(item.wav_path)


@bp.get("/api/video/<item_id>")
def api_video(item_id: str) -> object:
    item = ctx().store.require(item_id)
    if not item.preview_mp4 or not item.preview_mp4.exists():
        return jsonify({"error": "no video"}), 404
    return stream_file(item.preview_mp4)


@bp.get("/api/peaks/<item_id>")
def api_peaks(item_id: str) -> object:
    c = ctx()
    item = c.store.require(item_id)
    peaks = item.extra.get("peaks")
    if not peaks:
        peaks = compute_peaks(item.wav_path)
    return jsonify({"id": item.id, "duration": item.duration,
                    "sample_rate": item.sample_rate, "peaks": peaks})


# ── 导出 ─────────────────────────────────────────────────

@bp.post("/api/export")
def api_export() -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    item_id = body.get("item_id")
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400
    item = c.store.require(item_id)
    start = float(body.get("start", 0))
    end = float(body.get("end", item.duration))
    fmt = (body.get("format") or "wav").lower()
    sr = int(body.get("sample_rate", 32000))
    if fmt not in ("wav", "mp3"):
        return jsonify({"error": "仅支持 wav/mp3"}), 400
    ext = "wav" if fmt == "wav" else "mp3"
    codec = "pcm_s16le" if ext == "wav" else "libmp3lame"
    if end - start <= 0:
        return jsonify({"error": "无效选区"}), 400

    # 输出目录: 本地素材→源文件目录; B站/派生→workdir/exports
    src = Path(item.source) if item.source else None
    if src and src.exists():
        out_dir = src.parent
    else:
        out_dir = c.cfg.workdir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    base = Path(item.name).stem or "voicecut"
    fname = f"{base}_{c.fmt_ts(start)}-{c.fmt_ts(end)}.{ext}"
    path = c.unique_path(out_dir / fname)
    export_segment(item.wav_path, path, start, end, sample_rate=sr, codec=codec)

    dl_key = c.register_download(path)
    return jsonify({"name": path.name, "path": str(path),
                    "download_url": f"/api/files/{dl_key}"})


@bp.get("/api/files/<name>")
def api_files(name: str) -> object:
    c = ctx()
    c.prune_downloadable()
    with c.dl_lock:
        rec = c.downloadable.get(name)
    path = rec["path"] if rec else None
    if not path or not path.exists():
        return jsonify({"error": "not found"}), 404
    resp = stream_file(path)
    resp.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return resp


# ── 清洗 ─────────────────────────────────────────────────

@bp.post("/api/denoise")
def api_denoise() -> object:
    c = ctx()
    item = c.store.require(request.get_json(force=True).get("item_id"))
    tid = c.tasks.submit(_denoise_worker, c, item)
    return jsonify({"task_id": tid})


def _denoise_worker(c, src_item: MediaItem) -> dict:
    items_dir = c.cfg.workdir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    item_id = c.store.new_id()
    out = items_dir / f"{item_id}.wav"
    denoise_mod.denoise_wav(src_item.wav_path, out, stationary=True, prop_decrease=0.75)
    item = c.register_item(item_id=item_id, wav=out, name=f"{src_item.name}_降噪",
                           kind="denoised", source=str(out), derived_from=src_item.id,
                           project_id=src_item.project_id or None)
    return {"item_id": item.id, "item": c.item_json(item)}


@bp.post("/api/separate")
def api_separate() -> object:
    c = ctx()
    item = c.store.require(request.get_json(force=True).get("item_id"))
    tid = c.tasks.submit(_separate_worker, c, item, gpu=True)
    return jsonify({"task_id": tid})


def _separate_worker(c, src_item: MediaItem) -> dict:
    out_dir = c.cfg.workdir / "demucs" / src_item.id
    tid = c.tasks.current_task_id()
    res = separate_mod.run_separation(
        src_item.wav_path, out_dir, device="auto",
        task_id=tid, tasks=c.tasks,
    )
    items_dir = c.cfg.workdir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)

    def _reg(path: Path, label: str, kind: str) -> MediaItem:
        item_id = c.store.new_id()
        wav = items_dir / f"{item_id}.wav"
        extract_audio(path, wav, sample_rate=48000, channels=1)  # 统一工作格式
        return c.register_item(item_id=item_id, wav=wav, name=f"{src_item.name}_{label}",
                               kind=kind, source=str(wav), derived_from=src_item.id,
                               project_id=src_item.project_id or None)

    vocal = _reg(Path(res["vocals"]), "人声", "vocal")
    inst = _reg(Path(res["no_vocals"]), "伴奏", "instrumental")
    return {"item_ids": [vocal.id, inst.id],
            "items": [c.item_json(vocal), c.item_json(inst)]}


@bp.post("/api/trim")
def api_trim() -> object:
    c = ctx()
    item = c.store.require(request.get_json(force=True).get("item_id"))
    tid = c.tasks.submit(_trim_worker, c, item)
    return jsonify({"task_id": tid})


def _trim_worker(c, src_item: MediaItem) -> dict:
    items_dir = c.cfg.workdir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    item_id = c.store.new_id()
    out = items_dir / f"{item_id}.wav"
    trim_silence(src_item.wav_path, out, sample_rate=48000)
    item = c.register_item(item_id=item_id, wav=out, name=f"{src_item.name}_去静音",
                           kind="trimmed", source=str(out), derived_from=src_item.id,
                           project_id=src_item.project_id or None)
    return {"item_id": item.id, "item": c.item_json(item)}


# ── 转写 / 训练集 ─────────────────────────────────────────

@bp.post("/api/transcribe")
def api_transcribe() -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    item = c.store.require(body.get("item_id"))
    segs = body.get("segments") or []
    model = body.get("model") or "medium"
    if model not in ("medium", "large-v3"):
        return jsonify({"error": "仅支持 medium / large-v3"}), 400
    if not segs:
        return jsonify({"error": "无片段"}), 400
    tid = c.tasks.submit(_transcribe_worker, c, item, segs, model, gpu=True)
    return jsonify({"task_id": tid})


def _transcribe_worker(c, item: MediaItem, segs: list, model: str) -> dict:
    tid = c.tasks.current_task_id()
    clips = [(float(s["start"]), float(s["end"])) for s in segs]
    texts = transcribe_mod.transcribe_clips_full(
        item.wav_path, clips, language="ja", model=model,
        progress_cb=lambda p: c.tasks.update(tid, progress=p, message=f"transcribe {p*100:.0f}%"),
        cancelled_cb=lambda: c.tasks.cancelled(tid),
    )
    return {"texts": texts}


@bp.post("/api/dataset/export")
def api_dataset_export() -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    speaker = body.get("speaker") or "speaker"
    language = body.get("language") or "JP"
    out_dir = body.get("out_dir")

    if body.get("project_id"):
        clips = body.get("clips") or []
        if not clips:
            return jsonify({"error": "无片段"}), 400
        sources: dict[str, str | Path] = {
            i.id: i.wav_path for i in c.store.by_project(body["project_id"])
        }
        ds_segs = [
            dataset_mod.DatasetSegment(
                item_id=(s.get("item_id") or ""),
                start=float(s["start"]), end=float(s["end"]),
                text=(s.get("text") or "").strip(),
                language=(s.get("language") or language),
                speaker=(s.get("speaker") or speaker),
            )
            for s in clips
        ]
        out_dir = out_dir or str(c.cfg.workdir / "datasets" / f"{body['project_id']}_{int(time.time())}")
        layout = body.get("layout") or "flat"
        val_ratio = float(body.get("val_ratio") or 0.0)
        tid = c.tasks.submit(_dataset_worker, c, sources, ds_segs, Path(out_dir), layout, val_ratio)
        return jsonify({"task_id": tid})

    item = c.store.require(body.get("item_id"))
    segs = body.get("segments") or []
    if not segs:
        return jsonify({"error": "无片段"}), 400
    out_dir = out_dir or str(c.cfg.workdir / "datasets" / f"{item.id}_{int(time.time())}")
    ds_segs = [
        dataset_mod.DatasetSegment(
            start=float(s["start"]), end=float(s["end"]),
            text=(s.get("text") or "").strip(),
            language=(s.get("language") or language),
            speaker=(s.get("speaker") or speaker),
        )
        for s in segs
    ]
    tid = c.tasks.submit(_dataset_worker, c, item.wav_path, ds_segs, Path(out_dir))
    return jsonify({"task_id": tid})


def _dataset_worker(c, sources, ds_segs: list, out_dir: Path,
                    layout: str = "flat", val_ratio: float = 0.0) -> dict:
    tid = c.tasks.current_task_id()
    return dataset_mod.export_dataset(sources, ds_segs, out_dir,
                                      tasks=c.tasks, task_id=tid,
                                      layout=layout, val_ratio=val_ratio)

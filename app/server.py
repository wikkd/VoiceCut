"""Flask 后端：静态页 + 全部 REST API。

覆盖：导入 / 音频视频 Range 流 / 波形峰值 / 导出 / 降噪 / 分离 / 去静音 /
转写 / 训练集导出 / B站直链代理。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from app import bilibili as bilibili_mod
from app import db as db_mod
from app import autosplit as autosplit_mod
from app import dataset as dataset_mod
from app import project as project_mod
from app import speakers as speakers_mod
from app import denoise as denoise_mod
from app import separate as separate_mod
from app import subtitles as subtitles_mod
from app import transcribe as transcribe_mod
from app.audio_ops import compute_peaks
from app.config import AppConfig
from app.ffmpeg_util import (
    detect_silence, export_segment, extract_audio, media_duration,
    remux_preview, trim_silence,
)
from app.log import get_logger
from app.media_store import MediaItem, MediaStore
from app.streaming import stream_file
from app.tasks import TaskCancelled, TaskManager

STATIC_DIR = Path(__file__).parent / "static"

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".ts", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".aac", ".ogg", ".m4a", ".opus"}

_downloadable: dict[str, Path] = {}
_dl_lock = __import__("threading").RLock()


def create_app(cfg: AppConfig | None = None) -> Flask:
    cfg = cfg or AppConfig()
    app = Flask(__name__, static_folder=None)
    app.config["VC_CFG"] = cfg
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4GB 上传上限

    log = get_logger()
    store = MediaStore(cfg.workdir)
    tasks = TaskManager()
    app.extensions["vc_store"] = store
    app.extensions["vc_tasks"] = tasks

    # ── 通用助手 ──────────────────────────────────────────────

    def _item_json(item: MediaItem) -> dict:
        video_url = f"/api/video/{item.id}" if item.preview_mp4 else None
        if item.extra.get("proxy_url"):
            video_url = item.extra["proxy_url"]
        return {
            "id": item.id, "name": item.name, "kind": item.kind,
            "project_id": item.project_id or "",
            "duration": item.duration, "sample_rate": item.sample_rate,
            "audio_url": f"/api/audio/{item.id}",
            "video_url": video_url,
            "peaks_url": f"/api/peaks/{item.id}",
            "subs_url": f"/api/subtitles/{item.id}",
            "derived_from": item.derived_from,
            "source": item.source,
            "extra": _item_extra(item),
        }

    def _item_extra(item: MediaItem) -> dict:
        """Item extra without the heavy peaks payload (fetched via peaks_url)."""
        extra = dict(item.extra)
        extra.pop("peaks", None)
        return extra

    def _default_project_id() -> str:
        return db_mod.ensure_default_project(db_mod.get_conn(cfg.workdir))

    def _register_item(*, wav: Path, name: str, kind: str, item_id: str | None = None,
                       source: str = "", preview: Path | None = None,
                       derived_from: str | None = None,
                       project_id: str | None = None,
                       extra: dict | None = None) -> MediaItem:
        item_id = item_id or store.new_id()
        project_id = project_id or _default_project_id()
        duration = media_duration(wav)
        peaks = compute_peaks(wav)
        item = MediaItem(
            id=item_id, name=name, wav_path=wav, duration=duration,
            sample_rate=48000, preview_mp4=preview, source=source,
            derived_from=derived_from, kind=kind, project_id=project_id,
            extra={**(extra or {}), "peaks": peaks},
        )
        store.add(item)
        return item

    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        i = 2
        while True:
            cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
            if not cand.exists():
                return cand
            i += 1

    def _fmt_ts(t: float) -> str:
        t = max(0.0, float(t))
        m = int(t // 60)
        s = t - m * 60
        return f"{m:02d}-{s:04.1f}"

    # ── 静态页面 ──────────────────────────────────────────────

    @app.get("/")
    def index() -> object:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str) -> object:
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/api/health")
    def health() -> object:
        return jsonify({"ok": True, "version": "0.1.0", "stage": "03-features"})

    @app.get("/api/config")
    def api_config() -> object:
        return jsonify({
            "ffmpeg": cfg.ffmpeg_path,
            "sample_rates": list(cfg.sample_rates),
            "dataset_sample_rate": cfg.dataset_sample_rate,
            "workdir": str(cfg.workdir),
            "models": {"whisper": ["medium", "large-v3"], "demucs": "htdemucs"},
        })

    @app.get("/api/projects")
    def api_projects() -> object:
        conn = db_mod.get_conn(cfg.workdir)
        out = []
        for r in db_mod.fetch_project_records(conn):
            try:
                extra = json.loads(r["extra"] or "{}")
            except Exception:
                extra = {}
            out.append({
                "id": r["id"], "name": r["name"], "created": r["created"],
                "updated": r["updated"],
                "item_count": db_mod.count_items_in_project(conn, r["id"]),
                "character_count": len(extra.get("characters") or []),
            })
        return jsonify(out)

    @app.post("/api/projects")
    def api_projects_create() -> object:
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            name = "\u65b0\u9879\u76ee"  # 新项目
        pid = f"p-{uuid.uuid4().hex[:10]}"
        now = time.time()
        conn = db_mod.get_conn(cfg.workdir)
        db_mod.insert_project(conn, pid, name, now, now, {})
        return jsonify({"id": pid, "name": name, "created": now, "updated": now,
                        "item_count": 0, "character_count": 0})

    @app.get("/api/projects/<project_id>")
    def api_project_record_get(project_id: str) -> object:
        conn = db_mod.get_conn(cfg.workdir)
        rec = db_mod.fetch_project_record(conn, project_id)
        if rec is None:
            return jsonify({"error": "project not found"}), 404
        pool = project_mod.load_pool(cfg.workdir, project_id)
        return jsonify({
            "id": rec["id"], "name": rec["name"], "created": rec["created"],
            "updated": rec["updated"],
            "characters": pool["characters"],
            "items": [_item_json(i) for i in store.by_project(project_id)],
        })

    @app.post("/api/projects/<project_id>/rename")
    def api_project_rename(project_id: str) -> object:
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "\u540d\u79f0\u4e3a\u7a7a"}), 400  # 名称为空
        conn = db_mod.get_conn(cfg.workdir)
        if db_mod.fetch_project_record(conn, project_id) is None:
            return jsonify({"error": "project not found"}), 404
        db_mod.rename_project_record(conn, project_id, name)
        return jsonify({"ok": True})

    @app.delete("/api/projects/<project_id>")
    def api_project_delete(project_id: str) -> object:
        conn = db_mod.get_conn(cfg.workdir)
        if db_mod.fetch_project_record(conn, project_id) is None:
            return jsonify({"error": "project not found"}), 404
        if db_mod.count_items_in_project(conn, project_id):
            return jsonify({"error": "\u9879\u76ee\u975e\u7a7a\uff0c\u8bf7\u5148\u79fb\u9664\u7d20\u6750"}), 400
        db_mod.delete_project_record(conn, project_id)
        return jsonify({"ok": True})

    @app.get("/api/projects/<project_id>/characters")
    def api_pool_get(project_id: str) -> object:
        pool = project_mod.load_pool(cfg.workdir, project_id)
        return jsonify({"characters": pool["characters"]})

    @app.post("/api/projects/<project_id>/characters")
    def api_pool_save(project_id: str) -> object:
        body = request.get_json(force=True) or {}
        chars = body.get("characters")
        if not isinstance(chars, list):
            return jsonify({"error": "characters must be a list"}), 400
        project_mod.save_pool(cfg.workdir, project_id, chars)
        return jsonify({"ok": True})

    @app.get("/api/items")
    def list_items() -> object:
        project_id = request.args.get("project_id") or None
        items = store.by_project(project_id) if project_id else store.all()
        return jsonify([_item_json(i) for i in items])

    @app.delete("/api/items/<item_id>")
    def delete_item(item_id: str) -> object:
        item = store.get(item_id)
        store.remove(item_id)
        if item is not None:
            _delete_item_files(item)
        return jsonify({"ok": True})

    def _delete_item_files(item: MediaItem) -> None:
        """尽力删除素材工作文件（wav/预览/字幕/来源副本），失败静默。"""
        paths = {p for p in (item.wav_path, item.preview_mp4) if p}
        f = item.extra.get("subs_file")
        if f:
            paths.add(Path(f))
        src = Path(item.source) if item.source else None
        if src and src not in paths and src.exists() and str(src).startswith(str(cfg.workdir)):
            paths.add(src)
        for p in paths:
            try:
                if p and p.exists():
                    p.unlink()
            except OSError:
                pass
        project_mod.delete_project(cfg.workdir, item.id)

    @app.get("/api/tasks")
    def list_tasks() -> object:
        return jsonify(tasks.all())

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> object:
        t = tasks.get(task_id)
        return jsonify(t) if t else (jsonify({"error": "not found"}), 404)

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id: str) -> object:
        ok = tasks.cancel(task_id)
        return jsonify({"ok": ok})

    # ── 导入 ─────────────────────────────────────────────────

    @app.post("/api/import")
    def api_import() -> object:
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "缺少文件"}), 400
        filename = Path(f.filename).name
        ext = Path(filename).suffix.lower()
        upload_dir = cfg.workdir / "imports"
        upload_dir.mkdir(parents=True, exist_ok=True)
        raw_path = upload_dir / (uuid.uuid4().hex + ext)
        f.save(str(raw_path))
        project_id = request.form.get("project_id") or ""
        tid = tasks.submit(_import_worker, cfg, store, raw_path, filename, project_id)
        return jsonify({"task_id": tid, "name": filename})

    def _import_worker(cfg_: AppConfig, store_: MediaStore, raw_path: Path, filename: str,
                       project_id: str = "") -> dict:
        raw_path = Path(raw_path)
        stem = Path(filename).stem or "voicecut"
        ext = raw_path.suffix.lower()
        kind = "video" if ext in VIDEO_EXTS else "audio"
        items_dir = cfg_.workdir / "items"
        items_dir.mkdir(parents=True, exist_ok=True)

        item_id = store_.new_id()
        wav = items_dir / f"{item_id}.wav"
        extract_audio(raw_path, wav, sample_rate=48000, channels=1)
        preview = None
        if kind == "video":
            try:
                preview = remux_preview(raw_path, items_dir / f"{item_id}.preview.mp4")
            except Exception:  # noqa: BLE001
                preview = None
        subs_file = None
        if kind == "video":
            try:
                subs_file = subtitles_mod.extract_embedded_subtitles(
                    raw_path, items_dir / f"{item_id}.srt")
            except Exception:  # noqa: BLE001
                subs_file = None
        item = _register_item(item_id=item_id, wav=wav, preview=preview, name=stem, kind=kind,
                              source=str(raw_path), project_id=project_id or None)
        if subs_file:
            item.extra["subs_file"] = str(subs_file)
            store_.persist(item)
        log.info("imported %s -> %s (%s)", filename, item.id, kind)
        return {"item_id": item.id, "item": _item_json(item)}

    # ── 流式 ─────────────────────────────────────────────────

    @app.get("/api/audio/<item_id>")
    def api_audio(item_id: str) -> object:
        item = store.require(item_id)
        if not item.wav_path.exists():
            return jsonify({"error": "missing"}), 404
        return stream_file(item.wav_path)

    @app.get("/api/video/<item_id>")
    def api_video(item_id: str) -> object:
        item = store.require(item_id)
        if not item.preview_mp4 or not item.preview_mp4.exists():
            return jsonify({"error": "no video"}), 404
        return stream_file(item.preview_mp4)

    @app.get("/api/peaks/<item_id>")
    def api_peaks(item_id: str) -> object:
        item = store.require(item_id)
        peaks = item.extra.get("peaks")
        if not peaks:
            peaks = compute_peaks(item.wav_path)
        return jsonify({"id": item.id, "duration": item.duration,
                        "sample_rate": item.sample_rate, "peaks": peaks})


    # ---- subtitle routes ----

    def _load_subs(item: MediaItem) -> list[dict]:
        f = item.extra.get("subs_file")
        if not f or not Path(f).exists():
            return []
        return [s.to_dict() for s in subtitles_mod.parse_subtitle_file(f)]

    @app.get("/api/subtitles/<item_id>")
    def api_subtitles(item_id: str) -> object:
        item = store.require(item_id)
        return jsonify({"id": item.id, "subs": _load_subs(item)})

    @app.post("/api/subtitles/<item_id>")
    def api_subtitles_upload(item_id: str) -> object:
        item = store.require(item_id)
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "缺少字幕文件"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in (".srt", ".ass", ".ssa"):
            return jsonify({"error": "仅支持 .srt / .ass / .ssa 字幕"}), 400
        subs_dir = cfg.workdir / "subs"
        subs_dir.mkdir(parents=True, exist_ok=True)
        path = subs_dir / f"{item.id}{ext}"
        f.save(str(path))
        subs = subtitles_mod.parse_subtitle_file(path)
        if not subs:
            return jsonify({"error": "字幕为空或无法解析"}), 400
        item.extra["subs_file"] = str(path)
        store.persist(item)
        return jsonify({"count": len(subs), "subs": [s.to_dict() for s in subs]})

    @app.post("/api/subtitles/<item_id>/generate")
    def api_subtitles_generate(item_id: str) -> object:
        item = store.require(item_id)
        body = request.get_json(force=True) or {}
        model = (body.get("model") or "medium").lower()
        if model not in ("tiny", "base", "small", "medium", "large-v3"):
            return jsonify({"error": "未知模型"}), 400
        tid = tasks.submit(_subs_generate_worker, cfg, store, item, model, gpu=True)
        return jsonify({"task_id": tid})

    def _subs_generate_worker(cfg_: AppConfig, store_: MediaStore, item: MediaItem,
                              model: str) -> dict:
        tid = tasks.current_task_id()
        subs = transcribe_mod.transcribe_timed(
            item.wav_path, language="ja", model=model,
            progress_cb=lambda p: tasks.update(tid, progress=p,
                                               message=f"识别中 {p*100:.0f}%"),
        )
        if subs:
            subs_dir = cfg_.workdir / "subs"
            subs_dir.mkdir(parents=True, exist_ok=True)
            path = subtitles_mod.write_srt(subs_dir / f"{item.id}.srt", subs)
            item.extra["subs_file"] = str(path)
            store_.persist(item)
        return {"count": len(subs), "subs": subs, "kept_existing": not subs}

    # ── 导出 ─────────────────────────────────────────────────

    @app.post("/api/export")
    def api_export() -> object:
        body = request.get_json(force=True) or {}
        item_id = body.get("item_id")
        if not item_id:
            return jsonify({"error": "缺少 item_id"}), 400
        item = store.require(item_id)
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
            out_dir = cfg.workdir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)

        base = Path(item.name).stem or "voicecut"
        fname = f"{base}_{_fmt_ts(start)}-{_fmt_ts(end)}.{ext}"
        path = _unique_path(out_dir / fname)
        export_segment(item.wav_path, path, start, end, sample_rate=sr, codec=codec)

        dl_key = _register_download(path)
        return jsonify({"name": path.name, "path": str(path),
                        "download_url": f"/api/files/{dl_key}"})

    def _register_download(path: Path) -> str:
        key = uuid.uuid4().hex
        with _dl_lock:
            _downloadable[key] = path
        return key

    @app.get("/api/files/<name>")
    def api_files(name: str) -> object:
        with _dl_lock:
            path = _downloadable.get(name)
        if not path or not path.exists():
            return jsonify({"error": "not found"}), 404
        resp = stream_file(path)
        resp.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        return resp

    # ── 清洗 ─────────────────────────────────────────────────

    @app.post("/api/denoise")
    def api_denoise() -> object:
        item = store.require(request.get_json(force=True).get("item_id"))
        tid = tasks.submit(_denoise_worker, cfg, store, item)
        return jsonify({"task_id": tid})

    def _denoise_worker(cfg_: AppConfig, store_: MediaStore, src_item: MediaItem) -> dict:
        items_dir = cfg_.workdir / "items"
        items_dir.mkdir(parents=True, exist_ok=True)
        item_id = store_.new_id()
        out = items_dir / f"{item_id}.wav"
        denoise_mod.denoise_wav(src_item.wav_path, out, stationary=True, prop_decrease=0.75)
        item = _register_item(item_id=item_id, wav=out, name=f"{src_item.name}_降噪", kind="denoised",
                              source=str(out), derived_from=src_item.id,
                              project_id=src_item.project_id or None)
        return {"item_id": item.id, "item": _item_json(item)}

    @app.post("/api/separate")
    def api_separate() -> object:
        item = store.require(request.get_json(force=True).get("item_id"))
        tid = tasks.submit(_separate_worker, cfg, store, item, gpu=True)
        return jsonify({"task_id": tid})

    def _separate_worker(cfg_: AppConfig, store_: MediaStore, src_item: MediaItem) -> dict:
        out_dir = cfg_.workdir / "demucs" / src_item.id
        tid = tasks.current_task_id()
        res = separate_mod.run_separation(
            src_item.wav_path, out_dir, device="cuda",
            task_id=tid, tasks=tasks,
        )
        items_dir = cfg_.workdir / "items"
        items_dir.mkdir(parents=True, exist_ok=True)

        def _reg(path: Path, label: str, kind: str) -> MediaItem:
            item_id = store_.new_id()
            wav = items_dir / f"{item_id}.wav"
            extract_audio(path, wav, sample_rate=48000, channels=1)  # 统一工作格式
            return _register_item(item_id=item_id, wav=wav, name=f"{src_item.name}_{label}", kind=kind,
                                  source=str(wav), derived_from=src_item.id,
                                  project_id=src_item.project_id or None)

        vocal = _reg(Path(res["vocals"]), "人声", "vocal")
        inst = _reg(Path(res["no_vocals"]), "伴奏", "instrumental")
        return {"item_ids": [vocal.id, inst.id], "items": [_item_json(vocal), _item_json(inst)]}

    @app.post("/api/trim")
    def api_trim() -> object:
        item = store.require(request.get_json(force=True).get("item_id"))
        tid = tasks.submit(_trim_worker, cfg, store, item)
        return jsonify({"task_id": tid})

    def _trim_worker(cfg_: AppConfig, store_: MediaStore, src_item: MediaItem) -> dict:
        items_dir = cfg_.workdir / "items"
        items_dir.mkdir(parents=True, exist_ok=True)
        item_id = store_.new_id()
        out = items_dir / f"{item_id}.wav"
        trim_silence(src_item.wav_path, out, sample_rate=48000)
        item = _register_item(item_id=item_id, wav=out, name=f"{src_item.name}_去静音", kind="trimmed",
                              source=str(out), derived_from=src_item.id,
                              project_id=src_item.project_id or None)
        return {"item_id": item.id, "item": _item_json(item)}

    # ── 转写 / 训练集 ─────────────────────────────────────────

    @app.post("/api/transcribe")
    def api_transcribe() -> object:
        body = request.get_json(force=True) or {}
        item = store.require(body.get("item_id"))
        segs = body.get("segments") or []
        model = body.get("model") or "medium"
        if model not in ("medium", "large-v3"):
            return jsonify({"error": "仅支持 medium / large-v3"}), 400
        if not segs:
            return jsonify({"error": "无片段"}), 400
        tid = tasks.submit(_transcribe_worker, cfg, store, item, segs, model, gpu=True)
        return jsonify({"task_id": tid})

    def _transcribe_worker(cfg_: AppConfig, store_: MediaStore, item: MediaItem,
                           segs: list, model: str) -> dict:
        tid = tasks.current_task_id()
        clips = [(float(s["start"]), float(s["end"])) for s in segs]
        texts = transcribe_mod.transcribe_clips_full(
            item.wav_path, clips, language="ja", model=model,
            progress_cb=lambda p: tasks.update(tid, progress=p, message=f"transcribe {p*100:.0f}%"),
            cancelled_cb=lambda: tasks.cancelled(tid),
        )
        return {"texts": texts}

    @app.post("/api/dataset/export")
    def api_dataset_export() -> object:
        body = request.get_json(force=True) or {}
        speaker = body.get("speaker") or "speaker"
        language = body.get("language") or "JP"
        out_dir = body.get("out_dir")

        if body.get("project_id"):
            clips = body.get("clips") or []
            if not clips:
                return jsonify({"error": "无片段"}), 400
            sources: dict[str, str | Path] = {
                i.id: i.wav_path for i in store.by_project(body["project_id"])
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
            out_dir = out_dir or str(cfg.workdir / "datasets" / f"{body['project_id']}_{int(time.time())}")
            layout = body.get("layout") or "flat"
            val_ratio = float(body.get("val_ratio") or 0.0)
            tid = tasks.submit(_dataset_worker, sources, ds_segs, Path(out_dir), layout, val_ratio)
            return jsonify({"task_id": tid})

        item = store.require(body.get("item_id"))
        segs = body.get("segments") or []
        if not segs:
            return jsonify({"error": "无片段"}), 400
        out_dir = out_dir or str(cfg.workdir / "datasets" / f"{item.id}_{int(time.time())}")
        ds_segs = [
            dataset_mod.DatasetSegment(
                start=float(s["start"]), end=float(s["end"]),
                text=(s.get("text") or "").strip(),
                language=(s.get("language") or language),
                speaker=(s.get("speaker") or speaker),
            )
            for s in segs
        ]
        tid = tasks.submit(_dataset_worker, item.wav_path, ds_segs, Path(out_dir))
        return jsonify({"task_id": tid})

    def _dataset_worker(sources, ds_segs: list, out_dir: Path,
                         layout: str = "flat", val_ratio: float = 0.0) -> dict:
        tid = tasks.current_task_id()
        return dataset_mod.export_dataset(sources, ds_segs, out_dir,
                                          tasks=tasks, task_id=tid,
                                          layout=layout, val_ratio=val_ratio)

    # ── B 站 ─────────────────────────────────────────────────

    # ── 网络 URL 导入（多平台，yt-dlp 解析） ──

    def _open_url(url: str, project_id: str = "") -> dict:
        try:
            job = bilibili_mod.create_job(url)
        except RuntimeError as exc:
            return {"url": url, "ok": False, "error": str(exc)}
        tid = tasks.submit(_bilibili_worker, cfg, store, job, project_id)
        return {"url": url, "ok": True, "job_id": job["id"], "title": job["title"],
                "video_proxy_url": f"/api/bilibili/proxy/{job['id']}", "task_id": tid}

    def _split_urls(raw) -> list:
        if isinstance(raw, str):
            return [u.strip() for u in raw.replace("\r", "\n").split("\n") if u.strip()]
        out = []
        for u in (raw or []):
            if isinstance(u, str) and u.strip():
                out.append(u.strip())
        return out

    @app.post("/api/url/open")
    def api_url_open() -> object:
        body = request.get_json(force=True) or {}
        urls = _split_urls(body.get("urls"))
        if not urls:
            return jsonify({"error": "缺少链接"}), 400
        project_id = body.get("project_id") or ""
        return jsonify({"results": [_open_url(u, project_id) for u in urls]})

    @app.post("/api/bilibili/open")
    def api_bilibili_open() -> object:  # 旧入口别名
        body = request.get_json(force=True) or {}
        urls = _split_urls(body.get("url") or body.get("urls"))
        if not urls:
            return jsonify({"error": "缺少链接"}), 400
        project_id = body.get("project_id") or ""
        r = _open_url(urls[0], project_id)
        if not r["ok"]:
            return jsonify({"error": r["error"]}), 400
        return jsonify({k: r[k] for k in ("job_id", "title", "video_proxy_url", "task_id")})

    def _bilibili_worker(cfg_: AppConfig, store_: MediaStore, job: dict,
                         project_id: str = "") -> dict:
        tid = tasks.current_task_id()
        cache = cfg_.workdir / "bilibili"
        cache.mkdir(parents=True, exist_ok=True)
        if tasks:
            tasks.update(tid, progress=0.0, message="解析完成，开始获取音频")

        if job.get("audio_url"):
            src_file = bilibili_mod.download(job["audio_url"], cache / f"{job['id']}.{job.get('audio_ext') or 'm4a'}",
                                             referer=job["referer"], tasks=tasks, task_id=tid)
        else:
            # 降级: 下载整段视频再提取音频
            if not job.get("video_url"):
                raise RuntimeError("该视频无可用音视频流")
            src_file = bilibili_mod.download(job["video_url"], cache / f"{job['id']}.{job.get('video_ext') or 'mp4'}",
                                             referer=job["referer"], tasks=tasks, task_id=tid)

        items_dir = cfg_.workdir / "items"
        items_dir.mkdir(parents=True, exist_ok=True)
        item_id = store_.new_id()
        wav = items_dir / f"{item_id}.wav"
        extract_audio(src_file, wav, sample_rate=48000, channels=1)
        item = _register_item(item_id=item_id, wav=wav, name=job["title"], kind="url",
                              source=job["url"], project_id=project_id or None,
                              extra={"proxy_url": f"/api/bilibili/proxy/{job['id']}",
                                     "bilibili_job": job["id"]})
        log.info("url import ok: %s -> %s", job.get("title"), item.id)
        return {"item_id": item.id, "item": _item_json(item)}

    @app.get("/api/bilibili/proxy/<job_id>")
    def api_bilibili_proxy(job_id: str) -> object:
        job = bilibili_mod.get_job(job_id)
        if not job:
            return jsonify({"error": "job 不存在"}), 404
        return _proxy_once(job, retried=False)

    def _proxy_once(job: dict, retried: bool) -> Response:
        range_header = request.headers.get("Range")
        try:
            generator, status, headers = bilibili_mod.make_proxy(job, range_header)
        except bilibili_mod.ProxyStreamError as exc:
            if exc.status in (403, 404, 416) and not retried:
                try:
                    job = bilibili_mod.re_resolve(job)
                except RuntimeError:
                    pass
                return _proxy_once(job, retried=True)
            return jsonify({"error": str(exc)}), exc.status
        resp = Response(generator, status=status)
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    # ── 角色池 / 片段持久化（每素材 project.json） ──

    @app.get("/api/items/<item_id>/project")
    def api_project_get(item_id: str) -> object:
        store.require(item_id)
        return jsonify(project_mod.load_project(cfg.workdir, item_id))

    @app.post("/api/items/<item_id>/project")
    def api_project_save(item_id: str) -> object:
        store.require(item_id)
        body = request.get_json(force=True) or {}
        proj = project_mod.load_project(cfg.workdir, item_id)
        if "segments" in body:
            proj["segments"] = body["segments"] or []
        if "speaker_segments" in body:
            proj["speaker_segments"] = body["speaker_segments"] or []
        project_mod.save_project(cfg.workdir, item_id, proj)
        return jsonify({"ok": True})

    @app.post("/api/items/<item_id>/autosplit")
    def api_autosplit(item_id: str) -> object:
        store.require(item_id)
        body = request.get_json(force=True) or {}
        tid = tasks.submit(_autosplit_worker, cfg, store, item_id, body)
        return jsonify({"task_id": tid})

    def _autosplit_worker(cfg_: AppConfig, store_: MediaStore, item_id: str, body: dict) -> dict:
        tid = tasks.current_task_id()
        item = store_.require(item_id)
        threshold_db = float(body.get("threshold_db") or -35.0)
        min_silence = float(body.get("min_silence") or 0.5)
        min_len = float(body.get("min_len") or 0.8)
        max_len = float(body.get("max_len") or 15.0)
        language = body.get("language") or "JP"
        if tid and tasks.cancelled(tid):
            raise TaskCancelled()
        if tid:
            tasks.update(tid, progress=0.1, message="detecting silence")
        silences = detect_silence(item.wav_path, threshold_db=threshold_db, min_silence=min_silence)
        duration = item.duration or media_duration(item.wav_path)
        if tid:
            tasks.update(tid, progress=0.5, message="splitting by silence")
        clips = autosplit_mod.split_by_silence(duration, silences, min_len=min_len, max_len=max_len)
        segs = [{
            "id": project_mod.new_uid("seg"),
            "start": round(s, 3), "end": round(e, 3),
            "text": "", "language": language,
            "speakerLabel": "", "characterId": None, "note": "",
        } for s, e in clips]
        proj = project_mod.load_project(cfg_.workdir, item_id)
        proj["segments"] = segs
        project_mod.save_project(cfg_.workdir, item_id, proj)
        if tid:
            tasks.update(tid, progress=1.0, message="done")
        return {"count": len(segs), "clips": clips}

    @app.get("/api/items/<item_id>/speakers")
    def api_speakers_get(item_id: str) -> object:
        store.require(item_id)
        proj = project_mod.load_project(cfg.workdir, item_id)
        return jsonify({"speaker_segments": proj["speaker_segments"],
                        "characters": proj["characters"]})

    @app.post("/api/items/<item_id>/speakers/generate")
    def api_speakers_generate(item_id: str) -> object:
        store.require(item_id)
        tid = tasks.submit(_speakers_worker, cfg, store, item_id, gpu=True)
        return jsonify({"task_id": tid})

    def _speakers_worker(cfg_: AppConfig, store_: MediaStore, item_id: str) -> dict:
        tid = tasks.current_task_id()
        item = store_.require(item_id)
        subs = _load_subs(item)
        if not subs:
            tasks.update(tid, progress=0.05, message="未找到字幕，先执行 Whisper 识别…")
            subs = transcribe_mod.transcribe_timed(
                item.wav_path, language="ja", model="medium",
                progress_cb=lambda p: tasks.update(tid, progress=p * 0.3,
                                                   message=f"识别字幕 {p * 100:.0f}%"))
            if subs:
                subs_dir = cfg_.workdir / "subs"
                subs_dir.mkdir(parents=True, exist_ok=True)
                path = subtitles_mod.write_srt(subs_dir / f"{item.id}.srt", subs)
                item.extra["subs_file"] = str(path)
                store_.persist(item)
        if not subs:
            raise RuntimeError("未识别到语音内容，无法区分说话人")
        res = speakers_mod.generate_speakers(
            item.wav_path, subs,
            progress_cb=lambda p: tasks.update(tid, progress=0.3 + p * 0.6,
                                               message=f"说话人声纹聚类 {p * 100:.0f}%"),
        )
        speaker_segments = res["speaker_segments"]
        project_id = item.project_id or _default_project_id()
        pool = project_mod.load_pool(cfg_.workdir, project_id)
        label_embeds = res.get("label_embeddings") or {}
        if res.get("quality") == "ecapa" and label_embeds:
            assignments, chars, created = speakers_mod.match_labels_to_pool(
                item.id, label_embeds, pool["characters"])
            merged = max(0, len(assignments) - len(created))
        else:
            # MFCC fallback / no embeddings: one project character per label
            chars = pool["characters"]
            assignments = {}
            created = []
            for lb in sorted({s["label"] for s in speaker_segments if s.get("label")}):
                key = f"{item.id}:{lb}"
                existing = next((c for c in chars if key in (c.get("speakerLabels") or [])), None)
                if existing:
                    assignments[lb] = existing["id"]
                    continue
                cid = project_mod.new_uid("char")
                chars.append({
                    "id": cid, "name": lb, "color": project_mod.next_color(),
                    "speakerLabels": [key], "created": time.time(),
                })
                assignments[lb] = cid
                created.append(cid)
            merged = 0
        project_mod.save_pool(cfg_.workdir, project_id, chars)
        char_of_label = {lb: cid for lb, cid in assignments.items()}
        proj = project_mod.load_project(cfg_.workdir, item.id)
        for seg in proj["segments"]:
            lb = seg.get("speakerLabel")
            if lb and not seg.get("characterId") and lb in char_of_label:
                seg["characterId"] = char_of_label[lb]
        proj["speaker_segments"] = speaker_segments
        project_mod.save_project(cfg_.workdir, item.id, proj)
        return {"count": len(speaker_segments), "total": res["total"], "labeled": res["labeled"],
                "n_speakers": res["n_speakers"], "quality": res["quality"],
                "speaker_segments": speaker_segments,
                "characters": chars, "created": created, "merged": merged}

    # ── 素材重命名 ──

    @app.post("/api/items/<item_id>/rename")
    def api_item_rename(item_id: str) -> object:
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "名称为空"}), 400
        item = store.require(item_id)
        item.name = name
        store.persist(item)
        return jsonify(_item_json(item))

    return app

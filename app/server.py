"""Flask 后端：静态页 + 全部 REST API。

覆盖：导入 / 音频视频 Range 流 / 波形峰值 / 导出 / 降噪 / 分离 / 去静音 /
转写 / 训练集导出 / B站直链代理。
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from app import bilibili as bilibili_mod
from app import dataset as dataset_mod
from app import project as project_mod
from app import speakers as speakers_mod
from app import denoise as denoise_mod
from app import separate as separate_mod
from app import subtitles as subtitles_mod
from app import transcribe as transcribe_mod
from app.audio_ops import compute_peaks
from app.config import AppConfig
from app.ffmpeg_util import export_segment, extract_audio, media_duration, remux_preview, trim_silence
from app.media_store import MediaItem, MediaStore
from app.streaming import stream_file
from app.tasks import TaskManager

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

    store = MediaStore()
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
            "duration": item.duration, "sample_rate": item.sample_rate,
            "audio_url": f"/api/audio/{item.id}",
            "video_url": video_url,
            "peaks_url": f"/api/peaks/{item.id}",
            "subs_url": f"/api/subtitles/{item.id}",
            "derived_from": item.derived_from,
            "source": item.source,
            "extra": item.extra,
        }

    def _register_item(*, wav: Path, name: str, kind: str, source: str = "",
                       preview: Path | None = None, derived_from: str | None = None,
                       extra: dict | None = None) -> MediaItem:
        duration = media_duration(wav)
        peaks = compute_peaks(wav)
        item = MediaItem(
            id=store.new_id(), name=name, wav_path=wav, duration=duration,
            sample_rate=48000, preview_mp4=preview, source=source,
            derived_from=derived_from, kind=kind,
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

    @app.get("/api/items")
    def list_items() -> object:
        return jsonify([_item_json(i) for i in store.all()])

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
        tid = tasks.submit(_import_worker, cfg, store, raw_path, filename)
        return jsonify({"task_id": tid, "name": filename})

    def _import_worker(cfg_: AppConfig, store_: MediaStore, raw_path: Path, filename: str) -> dict:
        raw_path = Path(raw_path)
        stem = Path(filename).stem or "voicecut"
        ext = raw_path.suffix.lower()
        kind = "video" if ext in VIDEO_EXTS else "audio"
        items_dir = cfg_.workdir / "items"
        items_dir.mkdir(parents=True, exist_ok=True)

        new_id = store_.new_id()
        wav = items_dir / f"{new_id}.wav"
        extract_audio(raw_path, wav, sample_rate=48000, channels=1)
        preview = None
        if kind == "video":
            try:
                preview = remux_preview(raw_path, items_dir / f"{new_id}.preview.mp4")
            except Exception:  # noqa: BLE001
                preview = None
        subs_file = None
        if kind == "video":
            try:
                subs_file = subtitles_mod.extract_embedded_subtitles(
                    raw_path, items_dir / f"{new_id}.srt")
            except Exception:  # noqa: BLE001
                subs_file = None
        item = _register_item(wav=wav, preview=preview, name=stem, kind=kind,
                              source=str(raw_path))
        if subs_file:
            item.extra["subs_file"] = str(subs_file)
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
        return jsonify({"count": len(subs), "subs": [s.to_dict() for s in subs]})

    @app.post("/api/subtitles/<item_id>/generate")
    def api_subtitles_generate(item_id: str) -> object:
        item = store.require(item_id)
        body = request.get_json(force=True) or {}
        model = (body.get("model") or "medium").lower()
        if model not in ("tiny", "base", "small", "medium", "large-v3"):
            return jsonify({"error": "未知模型"}), 400
        tid = tasks.submit(_subs_generate_worker, cfg, store, item, model)
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
        new_id = store_.new_id()
        out = items_dir / f"{new_id}.wav"
        denoise_mod.denoise_wav(src_item.wav_path, out, stationary=True, prop_decrease=0.75)
        item = _register_item(wav=out, name=f"{src_item.name}_降噪", kind="denoised",
                              source=str(out), derived_from=src_item.id)
        return {"item_id": item.id, "item": _item_json(item)}

    @app.post("/api/separate")
    def api_separate() -> object:
        item = store.require(request.get_json(force=True).get("item_id"))
        tid = tasks.submit(_separate_worker, cfg, store, item)
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
            new_id = store_.new_id()
            wav = items_dir / f"{new_id}.wav"
            extract_audio(path, wav, sample_rate=48000, channels=1)  # 统一工作格式
            return _register_item(wav=wav, name=f"{src_item.name}_{label}", kind=kind,
                                  source=str(wav), derived_from=src_item.id)

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
        new_id = store_.new_id()
        out = items_dir / f"{new_id}.wav"
        trim_silence(src_item.wav_path, out, sample_rate=48000)
        item = _register_item(wav=out, name=f"{src_item.name}_去静音", kind="trimmed",
                              source=str(out), derived_from=src_item.id)
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
        tid = tasks.submit(_transcribe_worker, cfg, store, item, segs, model)
        return jsonify({"task_id": tid})

    def _transcribe_worker(cfg_: AppConfig, store_: MediaStore, item: MediaItem,
                           segs: list, model: str) -> dict:
        d = cfg_.workdir / "transcribe" / item.id
        d.mkdir(parents=True, exist_ok=True)
        tid = tasks.current_task_id()
        total = len(segs)
        texts: list[str] = []
        for i, s in enumerate(segs):
            if tasks:
                tasks.update(tid, progress=i / total if total else 1.0,
                             message=f"转写 {i + 1}/{total}: {Path(item.name).stem}")
            tmp = d / f"{i:03d}.wav"
            export_segment(item.wav_path, tmp, float(s["start"]), float(s["end"]),
                           sample_rate=16000)
            texts.append(transcribe_mod.transcribe_file(tmp, language="ja", model=model))
        if tasks:
            tasks.update(tid, progress=1.0, message="转写完成")
        return {"texts": texts}

    @app.post("/api/dataset/export")
    def api_dataset_export() -> object:
        body = request.get_json(force=True) or {}
        item = store.require(body.get("item_id"))
        segs = body.get("segments") or []
        if not segs:
            return jsonify({"error": "无片段"}), 400
        speaker = body.get("speaker") or "speaker"
        language = body.get("language") or "JP"
        out_dir = body.get("out_dir") or str(cfg.workdir / "datasets" / f"{item.id}_{int(time.time())}")
        ds_segs = [
            dataset_mod.DatasetSegment(
                start=float(s["start"]), end=float(s["end"]),
                text=(s.get("text") or "").strip(),
                language=(s.get("language") or language),
                speaker=(s.get("speaker") or speaker),
            )
            for s in segs
        ]
        tid = tasks.submit(_dataset_worker, item, ds_segs, Path(out_dir))
        return jsonify({"task_id": tid})

    def _dataset_worker(item: MediaItem, ds_segs: list, out_dir: Path) -> dict:
        return dataset_mod.export_dataset(item.wav_path, ds_segs, out_dir)

    # ── B 站 ─────────────────────────────────────────────────

    # ── 网络 URL 导入（多平台，yt-dlp 解析） ──

    def _open_url(url: str) -> dict:
        try:
            job = bilibili_mod.create_job(url)
        except RuntimeError as exc:
            return {"url": url, "ok": False, "error": str(exc)}
        tid = tasks.submit(_bilibili_worker, cfg, store, job)
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
        return jsonify({"results": [_open_url(u) for u in urls]})

    @app.post("/api/bilibili/open")
    def api_bilibili_open() -> object:  # 旧入口别名
        body = request.get_json(force=True) or {}
        urls = _split_urls(body.get("url") or body.get("urls"))
        if not urls:
            return jsonify({"error": "缺少链接"}), 400
        r = _open_url(urls[0])
        if not r["ok"]:
            return jsonify({"error": r["error"]}), 400
        return jsonify({k: r[k] for k in ("job_id", "title", "video_proxy_url", "task_id")})

    def _bilibili_worker(cfg_: AppConfig, store_: MediaStore, job: dict) -> dict:
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
        new_id = store_.new_id()
        wav = items_dir / f"{new_id}.wav"
        extract_audio(src_file, wav, sample_rate=48000, channels=1)
        item = _register_item(wav=wav, name=job["title"], kind="url",
                              source=job["url"],
                              extra={"proxy_url": f"/api/bilibili/proxy/{job['id']}",
                                     "bilibili_job": job["id"]})
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
        if "characters" in body:
            proj["characters"] = body["characters"] or []
        if "segments" in body:
            proj["segments"] = body["segments"] or []
        if "speaker_segments" in body:
            proj["speaker_segments"] = body["speaker_segments"] or []
        project_mod.save_project(cfg.workdir, item_id, proj)
        return jsonify({"ok": True})

    @app.get("/api/items/<item_id>/speakers")
    def api_speakers_get(item_id: str) -> object:
        store.require(item_id)
        proj = project_mod.load_project(cfg.workdir, item_id)
        return jsonify({"speaker_segments": proj["speaker_segments"],
                        "characters": proj["characters"]})

    @app.post("/api/items/<item_id>/speakers/generate")
    def api_speakers_generate(item_id: str) -> object:
        store.require(item_id)
        tid = tasks.submit(_speakers_worker, cfg, store, item_id)
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
        if not subs:
            raise RuntimeError("未识别到语音内容，无法区分说话人")
        res = speakers_mod.generate_speakers(
            item.wav_path, subs,
            progress_cb=lambda p: tasks.update(tid, progress=0.3 + p * 0.6,
                                               message=f"说话人声纹聚类 {p * 100:.0f}%"),
        )
        proj = project_mod.load_project(cfg_.workdir, item.id)
        speaker_segments = res["speaker_segments"]
        existing = {}
        for c in proj["characters"]:
            for lb in (c.get("speakerLabels") or []):
                existing[lb] = c["id"]
        created = []
        for lb in sorted({s["label"] for s in speaker_segments if s.get("label")}):
            if lb in existing:
                continue
            cid = project_mod.new_uid("char")
            proj["characters"].append({
                "id": cid, "name": lb, "color": project_mod.next_color(),
                "speakerLabels": [lb], "created": time.time(),
            })
            existing[lb] = cid
            created.append(cid)
        char_of_label = {lb: cid for c in proj["characters"] for lb in (c.get("speakerLabels") or [])}
        for seg in proj["segments"]:
            lb = seg.get("speakerLabel")
            if lb and char_of_label.get(lb) and not seg.get("characterId"):
                seg["characterId"] = char_of_label[lb]
        proj["speaker_segments"] = speaker_segments
        project_mod.save_project(cfg_.workdir, item.id, proj)
        return {"count": len(speaker_segments), "total": res["total"], "labeled": res["labeled"],
                "n_speakers": res["n_speakers"], "quality": res["quality"],
                "speaker_segments": speaker_segments,
                "characters": proj["characters"], "created": created}

    # ── 素材重命名 ──

    @app.post("/api/items/<item_id>/rename")
    def api_item_rename(item_id: str) -> object:
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "名称为空"}), 400
        item = store.require(item_id)
        item.name = name
        return jsonify(_item_json(item))

    return app

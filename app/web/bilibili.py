"""网络 URL / B站直链导入与代理路由（Blueprint）。

导入即后台下载：请求线程只登记 URL，解析与下载都在后台任务里完成，
音频先行成为可剪辑素材，完整视频随后补成本地预览。
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request

from app import bilibili as bilibili_mod
from app.ffmpeg_util import extract_audio, remux_preview
from app.web.media import VIDEO_EXTS

bp = Blueprint("bilibili", __name__)


def ctx():
    return current_app.extensions["vc_ctx"]


def _split_urls(raw) -> list:
    if isinstance(raw, str):
        return [u.strip() for u in raw.replace("\r", "\n").split("\n") if u.strip()]
    out = []
    for u in (raw or []):
        if isinstance(u, str) and u.strip():
            out.append(u.strip())
    return out


def _open_url(c, url: str, project_id: str = "") -> dict:
    """登记 URL 并提交后台下载任务（不在请求线程里解析）。

    导入后立即后台完成：① 音频流 → WAV → 可剪辑素材（快）
    ② 完整视频 → 本地预览 MP4（此后不再依赖会过期的远端直链）。
    """
    job = bilibili_mod.register_job(url)
    tid = c.tasks.submit(_bilibili_worker, c, job, project_id, kind="import")
    return {"url": url, "ok": True, "job_id": job["id"], "title": job["title"],
            "video_proxy_url": f"/api/bilibili/proxy/{job['id']}", "task_id": tid}


@bp.post("/api/url/open")
def api_url_open() -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    urls = _split_urls(body.get("urls"))
    if not urls:
        return jsonify({"error": "缺少链接"}), 400
    project_id = body.get("project_id") or ""
    return jsonify({"results": [_open_url(c, u, project_id) for u in urls]})


@bp.post("/api/bilibili/open")
def api_bilibili_open() -> object:  # 旧入口别名
    c = ctx()
    body = request.get_json(force=True) or {}
    urls = _split_urls(body.get("url") or body.get("urls"))
    if not urls:
        return jsonify({"error": "缺少链接"}), 400
    project_id = body.get("project_id") or ""
    r = _open_url(c, urls[0], project_id)
    if not r["ok"]:
        return jsonify({"error": r["error"]}), 400
    return jsonify({k: r[k] for k in ("job_id", "title", "video_proxy_url", "task_id")})


def _bilibili_worker(c, job: dict, project_id: str = "") -> dict:
    """导入即后台下载：解析 → 音频先行出素材 → 完整视频补本地预览。"""
    tid = c.tasks.current_task_id()
    cache = c.cfg.workdir / "bilibili"
    cache.mkdir(parents=True, exist_ok=True)
    items_dir = c.cfg.workdir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)

    # ── 0) 解析（yt-dlp；在后台线程里做，不阻塞导入请求）──
    if c.tasks:
        c.tasks.update(tid, progress=0.0, message="解析链接…")
    job = bilibili_mod.resolve_job(job)

    # ── 1) 音频优先：小、快，先让素材可剪辑、波形秒出 ──
    if job.get("audio_url"):
        src_file = bilibili_mod.download(
            job["audio_url"], cache / f"{job['id']}.{job.get('audio_ext') or 'm4a'}",
            referer=job["referer"], tasks=c.tasks, task_id=tid)
    elif job.get("video_url"):
        # 无独立音频流：用视频流兜底（下面还会复用同一文件做本地预览）
        src_file = bilibili_mod.download(
            job["video_url"], cache / f"{job['id']}.{job.get('video_ext') or 'mp4'}",
            referer=job["referer"], tasks=c.tasks, task_id=tid, label="下载媒体")
    else:
        # 直链里没有可用流（如分离流站）：交给 yt-dlp 直接下载视频
        src_file = bilibili_mod._download_with_ytdlp(job, cache, c.tasks, tid)

    if c.tasks:
        c.tasks.update(tid, progress=0.5, message="提取音频…")
    item_id = c.store.new_id()
    wav = items_dir / f"{item_id}.wav"
    extract_audio(src_file, wav, sample_rate=48000, channels=1)
    # proxy 直链仅当解析出单文件视频流时才有意义；DASH 分离流（番剧等）
    # 无单文件流，proxy 必然 404，宁可先不给，等本地预览补全
    extra = {"bilibili_job": job["id"]}
    if job.get("video_url"):
        extra["proxy_url"] = f"/api/bilibili/proxy/{job['id']}"
    item = c.register_item(item_id=item_id, wav=wav, name=job["title"], kind="url",
                           source=job["url"], project_id=project_id or None,
                           extra=extra)
    c.log.info("url import audio ok: %s -> %s", job.get("title"), item.id)

    # ── 2) 完整视频 → 本地预览（失败不影响已可用的音频素材）──
    if c.tasks:
        c.tasks.update(tid, progress=0.6, message="下载完整视频…")
    _attach_local_video(c, job, item, src_file, cache, items_dir)

    result = {"item_id": item.id, "item": c.item_json(item)}
    from app.web.projects import submit_project_analyze  # 延迟导入避免循环依赖
    auto_tid = submit_project_analyze(c, item.project_id or "")
    if auto_tid:
        result["auto_task_id"] = auto_tid
    return result


def _attach_local_video(c, job: dict, item, audio_src, cache, items_dir) -> None:
    """下载完整视频并转封装为本地预览 MP4，挂到素材上（尽力而为）。

    完成后播放/预览走本地 Range 流（/api/video/<id>），不再依赖会过期的
    远端签名直链；失败仅记录，不影响已可用的音频素材。
    """
    try:
        src = audio_src if Path(audio_src).suffix.lower() in VIDEO_EXTS \
            else bilibili_mod.download_video(job, cache, tasks=c.tasks,
                                             task_id=c.tasks.current_task_id())
        preview = remux_preview(src, items_dir / f"{item.id}.preview.mp4")
        item.preview_mp4 = preview
        item.extra["video_file"] = str(src)
        item.extra.pop("proxy_url", None)  # 有本地预览后不再需要代理流
        c.store.persist(item)
        c.log.info("url import video ok: %s -> %s", job.get("title"), preview.name)
    except Exception as exc:  # noqa: BLE001
        c.log.warning("视频下载/预览失败（音频素材仍可用）: %s", exc)


@bp.post("/api/bilibili/attach_video/<item_id>")
def api_attach_video(item_id: str) -> object:
    """补全/重试本地视频预览（导入时视频下载失败后的修复入口）。"""
    c = ctx()
    item = c.store.require(item_id)
    if not item.source or "bilibili.com" not in item.source:
        return jsonify({"error": "该素材没有 B 站来源链接，无法补全视频"}), 400
    if item.preview_mp4 and item.preview_mp4.exists():
        return jsonify({"ok": True, "task_id": None,
                        "video_url": f"/api/video/{item_id}", "note": "已有本地预览"})
    tid = c.tasks.submit(_attach_video_worker, c, item, kind="import")
    return jsonify({"ok": True, "task_id": tid})


def _attach_video_worker(c, item) -> dict:
    """重新解析来源 URL → 下载完整视频 → 转封装挂为本地预览。"""
    tid = c.tasks.current_task_id()
    c.tasks.update(tid, progress=0.0, message="重新解析链接…")
    job = bilibili_mod.resolve_job(bilibili_mod.register_job(item.source))
    cache = c.cfg.workdir / "bilibili"
    cache.mkdir(parents=True, exist_ok=True)
    items_dir = c.cfg.workdir / "items"
    # 音频是 wav，不在 VIDEO_EXTS → _attach_local_video 会走完整视频下载
    _attach_local_video(c, job, item, item.wav_path, cache, items_dir)
    if not item.preview_mp4 or not item.preview_mp4.exists():
        raise RuntimeError("视频补全失败（下载或转封装错误），可稍后重试")
    return {"item_id": item.id, "video_url": f"/api/video/{item.id}"}


@bp.get("/api/bilibili/proxy/<job_id>")
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

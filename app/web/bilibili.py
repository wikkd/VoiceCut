"""网络 URL / B站直链导入与代理路由（Blueprint）。"""
from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify, request

from app import bilibili as bilibili_mod
from app.ffmpeg_util import extract_audio

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
    try:
        job = bilibili_mod.create_job(url)
    except RuntimeError as exc:
        return {"url": url, "ok": False, "error": str(exc)}
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
    tid = c.tasks.current_task_id()
    cache = c.cfg.workdir / "bilibili"
    cache.mkdir(parents=True, exist_ok=True)
    if c.tasks:
        c.tasks.update(tid, progress=0.0, message="解析完成，开始获取音频")

    if job.get("audio_url"):
        src_file = bilibili_mod.download(
            job["audio_url"], cache / f"{job['id']}.{job.get('audio_ext') or 'm4a'}",
            referer=job["referer"], tasks=c.tasks, task_id=tid)
    else:
        # 降级: 下载整段视频再提取音频
        if not job.get("video_url"):
            raise RuntimeError("该视频无可用音视频流")
        src_file = bilibili_mod.download(
            job["video_url"], cache / f"{job['id']}.{job.get('video_ext') or 'mp4'}",
            referer=job["referer"], tasks=c.tasks, task_id=tid)

    items_dir = c.cfg.workdir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    item_id = c.store.new_id()
    wav = items_dir / f"{item_id}.wav"
    extract_audio(src_file, wav, sample_rate=48000, channels=1)
    item = c.register_item(item_id=item_id, wav=wav, name=job["title"], kind="url",
                           source=job["url"], project_id=project_id or None,
                           extra={"proxy_url": f"/api/bilibili/proxy/{job['id']}",
                                  "bilibili_job": job["id"]})
    c.log.info("url import ok: %s -> %s", job.get("title"), item.id)
    result = {"item_id": item.id, "item": c.item_json(item)}
    from app.web.projects import submit_project_analyze  # 延迟导入避免循环依赖
    auto_tid = submit_project_analyze(c, item.project_id or "")
    if auto_tid:
        result["auto_task_id"] = auto_tid
    return result


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

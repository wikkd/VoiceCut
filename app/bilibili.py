"""B 站直链代理（方案 A：免登录）。

机制:
- yt-dlp 解析: ① 单文件 MP4(视音频一体, 用于页面视频预览代理) ② audio-only 流(小、快, 用于秒出波形)
- /api/bilibili/proxy/<job_id>: 带 Referer/UA 的 Range 透传代理, URL 过期自动重解析一次
- 音频 job 由 server 侧后台下载 audio-only → 提取 WAV → 成为可剪辑素材

限制: 免登录通常仅 360p/480p; 720p+ 需 SESSDATA cookie(后续扩展)。
"""
from __future__ import annotations

import shutil
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app.tasks import TaskCancelled, TaskManager

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"


def referer_for(url: str) -> str:
    """Derive a Referer for a given URL host (multi-platform support)."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc or ""
    return f"https://{host}/" if host else REFERER

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ── yt-dlp 解析 ─────────────────────────────────────────────

def resolve_formats(url: str, *, cookies: dict | None = None) -> dict:
    """用 yt-dlp 解析视频信息，挑选最佳 单文件MP4 与 audio-only 流。"""
    import yt_dlp  # 延迟导入

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"yt-dlp 解析失败: {exc}") from exc

    formats = info.get("formats") or []
    single = [f for f in formats
              if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
              and f.get("url")]
    audio_only = [f for f in formats
                  if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
                  and f.get("url")]

    def _pick(fs, key):
        return max(fs, key=lambda f: f.get(key) or 0) if fs else None

    video = _pick(single, "height")
    audio = _pick(audio_only, "abr") or _pick(audio_only, "tbr")

    return {
        "title": info.get("title") or "",
        "duration": info.get("duration"),
        "id": info.get("id"),
        "video": {"format_id": video.get("format_id"), "ext": video.get("ext"),
                  "height": video.get("height"), "url": video.get("url")} if video else None,
        "audio": {"format_id": audio.get("format_id"), "ext": audio.get("ext"),
                  "abr": audio.get("abr"), "url": audio.get("url")} if audio else None,
    }


# ── job 注册表 ──────────────────────────────────────────────

def create_job(url: str) -> dict:
    """解析 URL 并注册代理 job。失败抛 RuntimeError（携带原因）。"""
    info = resolve_formats(url)
    job = {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "title": info["title"] or url,
        "video_url": info["video"]["url"] if info["video"] else None,
        "video_ext": info["video"]["ext"] if info["video"] else None,
        "audio_url": info["audio"]["url"] if info["audio"] else None,
        "audio_ext": info["audio"]["ext"] if info["audio"] else None,
        "referer": referer_for(url),
        "status": "resolved",
        "error": None,
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
    return job


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def re_resolve(job: dict) -> dict:
    """URL 过期时重新解析，原地更新 job。"""
    info = resolve_formats(job["url"])
    video = info["video"]
    audio = info["audio"]
    with _jobs_lock:
        cur = _jobs.get(job["id"])
        if cur is not None:
            cur["video_url"] = video["url"] if video else None
            cur["audio_url"] = audio["url"] if audio else None
            cur["status"] = "resolved"
            job = cur
    return job


# ── 下载（音频流）───────────────────────────────────────────

def download(
    url: str,
    dest: str | Path,
    *,
    referer: str = REFERER,
    tasks: TaskManager | None = None,
    task_id: str | None = None,
    chunk: int = 256 * 1024,
) -> Path:
    """下载 URL 到 dest，带进度上报。失败抛 RuntimeError。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"下载失败 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"下载失败: {exc.reason}") from exc

    total = int(resp.headers.get("Content-Length") or 0)
    done = 0
    with open(dest, "wb") as f:
        while True:
            if tasks and task_id and tasks.cancelled(task_id):
                raise TaskCancelled()
            data = resp.read(chunk)
            if not data:
                break
            f.write(data)
            done += len(data)
            if total and tasks and task_id:
                tasks.update(task_id, progress=min(1.0, done / total),
                             message=f"下载音频 {done / 1e6:.1f}/{total / 1e6:.1f} MB")
    return dest


# ── Range 透传代理 ──────────────────────────────────────────

class ProxyStreamError(RuntimeError):
    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"上游 {status}")
        self.status = status


def make_proxy(job: dict, range_header: str | None, *, retried: bool = False):
    """请求上游直链（带 Referer），返回 (generator, status, headers)。

    Range 头原样透传；上游 403/416 视为 URL 过期，由调用方 re_resolve 后重试。
    """
    url = job["video_url"]
    if not url:
        raise ProxyStreamError(404, "该视频无单文件流，已降级为仅音频")

    headers = {"User-Agent": UA, "Referer": job["referer"]}
    if range_header:
        headers["Range"] = range_header
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        raise ProxyStreamError(exc.code) from exc
    except urllib.error.URLError as exc:
        raise ProxyStreamError(502, f"上游连接失败: {exc.reason}") from exc

    def generate():
        try:
            while True:
                data = resp.read(256 * 1024)
                if not data:
                    break
                yield data
        finally:
            resp.close()

    out_headers = {}
    for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
              "Content-Disposition", "Cache-Control"):
        v = resp.headers.get(k)
        if v:
            out_headers[k] = v
    return generate(), resp.status, out_headers

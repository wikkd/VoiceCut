"""网络 URL 导入（B 站 / 多平台）：后台下载到本地 + 直链 Range 代理（兼容保留）。

机制（导入即后台下载）:
- 导入时只登记 job，不在 HTTP 线程里解析；真正的解析与下载都在后台任务里完成。
- 后台先取音频流（小、快）→ 提取 WAV → 立刻成为可剪辑素材（波形秒出）。
- 随后下载完整视频 → 无损转封装为本地预览 MP4；此后播放/预览走本地 Range 流，
  不再依赖会过期的远端签名直链。
- /api/bilibili/proxy/<job_id> 的 Range 透传代理保留（下载完成前的即时预览 / 兼容），
  URL 过期会自动重解析一次。

下载引擎:
- 默认 yt-dlp（免登录通常仅 360p/480p，这是 B 站 API 限制）。
- 若 PATH 上存在 BBDown / yutto，或配置了 cookie，则优先走外部下载器（可拿高画质）。
- 外部下载器可用 ``VC_BBDOWN`` / ``VC_YUTTO`` 指定可执行文件路径。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from app.tasks import TaskCancelled, TaskManager

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"

# ── cookie / 外部下载器（可选，用于高画质）─────────────────

#: 环境变量名 → 读取顺序；SESSDATA 是 B 站登录态的关键 cookie
COOKIE_ENV = ("VC_BILIBILI_COOKIE", "VC_SESSDATA")


def bilibili_cookie() -> str:
    """可选的 B 站 cookie（``SESSDATA=...``）。未配置返回空串。

    配置后 yt-dlp 会带上登录态，通常可解锁 720p/1080p；外部下载器同样使用。
    """
    for name in COOKIE_ENV:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        # 允许直接写 SESSDATA 的值（自动补键名）
        return raw if "=" in raw else f"SESSDATA={raw}"
    return ""


def sessdata() -> str:
    """从 cookie 串里取出 SESSDATA 值（外部下载器用）。"""
    for part in bilibili_cookie().split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip().upper() == "SESSDATA" and v:
            return v.strip()
    return ""


# ── 下载引擎注册表 ──────────────────────────────────────────
# 新增引擎只需加一条描述（env / exe / 参数构造器），无需改探测与分发逻辑。

def _bbdown_args(exe: str, job: dict, out_dir: Path, sd: str) -> list[str]:
    cmd = [exe, job["url"], "--work-dir", str(out_dir), "--file-pattern", job["id"]]
    if sd:
        cmd += ["--cookie", f"SESSDATA={sd}"]
    return cmd


def _yutto_args(exe: str, job: dict, out_dir: Path, sd: str) -> list[str]:
    cmd = [exe, job["url"], "-d", str(out_dir), "--no-danmaku"]
    if sd:
        cmd += ["-c", sd]
    return cmd


_ENGINES = (
    {"name": "bbdown", "env": "VC_BBDOWN", "exe": "BBDown", "build_args": _bbdown_args},
    {"name": "yutto", "env": "VC_YUTTO", "exe": "yutto", "build_args": _yutto_args},
)


def detect_external_downloader() -> tuple[str, str] | None:
    """探测可用的外部下载器，返回 (名称, 可执行文件路径)。

    遍历 ``_ENGINES`` 注册表：显式环境变量路径优先，其次 PATH。
    都不可用时返回 None（回退到内置 yt-dlp）。
    """
    for eng in _ENGINES:
        explicit = os.environ.get(eng["env"], "").strip()
        path = explicit or shutil.which(eng["exe"]) or shutil.which(eng["exe"].lower())
        if path and Path(path).exists():
            return eng["name"], str(path)
    return None


def downloader_status() -> dict:
    """当前下载能力（供 /api/config 展示）。"""
    ext = detect_external_downloader()
    return {
        "engine": ext[0] if ext else "yt-dlp",
        "external_path": ext[1] if ext else None,
        "cookie": bool(bilibili_cookie()),
    }


def referer_for(url: str) -> str:
    """Derive a Referer for a given URL host (multi-platform support)."""
    from urllib.parse import urlparse
    host = urlparse(url).netloc or ""
    return f"https://{host}/" if host else REFERER

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 24 * 3600.0  # 代理 job 保留 24h，过期惰性清理


def _prune_jobs() -> None:
    """删除超过 TTL 的代理 job（惰性，避免内存无限增长）。"""
    now = time.time()
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items()
                    if now - float(v.get("created") or 0) > _JOB_TTL]:
            _jobs.pop(jid, None)


# ── yt-dlp 解析 ─────────────────────────────────────────────

def resolve_formats(url: str, *, cookies: dict | None = None) -> dict:
    """用 yt-dlp 解析视频信息，挑选最佳 单文件MP4 与 audio-only 流。

    若配置了 ``VC_BILIBILI_COOKIE`` / ``VC_SESSDATA``，解析会带登录态
    （通常可解锁 720p/1080p）。
    """
    import yt_dlp  # 延迟导入

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    cookie = bilibili_cookie()
    if cookie:
        opts["http_headers"] = {"Cookie": cookie, "User-Agent": UA}
    if cookies:
        opts["http_headers"] = {**(opts.get("http_headers") or {}), **cookies}
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

def _new_job(url: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "title": url,
        "video_url": None,
        "video_ext": None,
        "audio_url": None,
        "audio_ext": None,
        "referer": referer_for(url),
        "status": "registered",
        "error": None,
        "created": time.time(),
    }


def register_job(url: str) -> dict:
    """只登记一个 URL（不在 HTTP 线程里解析），解析交给后台任务。

    这样批量导入时接口立即返回，不会因逐个 yt-dlp 解析而阻塞请求线程。
    """
    job = _new_job(url)
    _prune_jobs()
    with _jobs_lock:
        _jobs[job["id"]] = job
    return job


def resolve_job(job: dict) -> dict:
    """解析 URL 并就地填充 job 的流信息与标题；失败抛 RuntimeError。"""
    info = resolve_formats(job["url"])
    with _jobs_lock:
        cur = _jobs.get(job["id"]) or job
        cur["title"] = info["title"] or cur.get("title") or job["url"]
        cur["duration"] = info.get("duration")
        cur["video_url"] = info["video"]["url"] if info["video"] else None
        cur["video_ext"] = info["video"]["ext"] if info["video"] else None
        cur["video_height"] = info["video"]["height"] if info["video"] else None
        cur["audio_url"] = info["audio"]["url"] if info["audio"] else None
        cur["audio_ext"] = info["audio"]["ext"] if info["audio"] else None
        cur["status"] = "resolved"
        _jobs[job["id"]] = cur
        return dict(cur)


def create_job(url: str) -> dict:
    """解析 URL 并注册 job（register + resolve 的兼容包装）。"""
    return resolve_job(register_job(url))


def get_job(job_id: str) -> dict | None:
    _prune_jobs()
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
    label: str = "下载音频",
) -> Path:
    """下载 URL 到 dest，带进度上报。失败抛 RuntimeError。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA, "Referer": referer}
    cookie = bilibili_cookie()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
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
                             message=f"{label} {done / 1e6:.1f}/{total / 1e6:.1f} MB")
    return dest


# ── 下载（完整视频，用于本地预览）───────────────────────────

def download_video(
    job: dict,
    out_dir: str | Path,
    *,
    tasks: TaskManager | None = None,
    task_id: str | None = None,
) -> Path:
    """把完整视频下载到 out_dir（含音轨），返回本地文件路径。

    引擎优先级：外部下载器（BBDown / yutto，若可用）→ 直链下载（单文件流）
    → yt-dlp CLI。下载完的视频用于本地预览，之后播放不再依赖会过期的直链。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = detect_external_downloader()
    if ext is not None:
        name, path = ext
        try:
            return _download_external(name, path, job, out_dir, tasks, task_id)
        except Exception:  # noqa: BLE001
            # 外部下载器失败时回退到内置路径，保证导入仍能完成
            if tasks and task_id:
                tasks.update(task_id, message=f"{name} 失败，回退 yt-dlp 下载视频…")

    if job.get("video_url"):
        dst = out_dir / f"{job['id']}.{job.get('video_ext') or 'mp4'}"
        return download(job["video_url"], dst, referer=job.get("referer") or REFERER,
                        tasks=tasks, task_id=task_id, label="下载视频")

    return _download_with_ytdlp(job, out_dir, tasks, task_id)


def _ytdlp_base_args() -> list[str]:
    """yt-dlp CLI 公共参数（含可选 cookie）。"""
    import sys

    args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--quiet"]
    cookie = bilibili_cookie()
    if cookie:
        args += ["--add-header", f"Cookie:{cookie}"]
    return args


def _download_with_ytdlp(
    job: dict,
    out_dir: Path,
    tasks: TaskManager | None,
    task_id: str | None,
) -> Path:
    """用 yt-dlp CLI 下载最佳视频（自动合并视音频），返回产物路径。

    B 站（及其它 DASH 站点）只有分离的 video-only / audio-only 流，必须靠
    yt-dlp 合并。这里严格要求 **成功率且产物真的是视频**：只看「有没有文件」
    会把「视频流下载失败后剩下的音频分片」误当成视频（实测该站视频 CDN 可能
    连不上、而音频正常），从而生成一个只有声音的“预览”。
    """
    if tasks and task_id:
        tasks.update(task_id, message="下载完整视频（yt-dlp）…")
    tmpl = str(out_dir / f"{job['id']}.%(ext)s")
    cmd = _ytdlp_base_args() + [
        "-f", "bv*+ba/b",          # 最佳视频+音频；不可用时退最佳单文件
        "--merge-output-format", "mp4",
        "-o", tmpl,
        job["url"],
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if tasks and task_id and tasks.cancelled(task_id):
        raise TaskCancelled()

    tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
    if proc.returncode != 0:
        _cleanup_ytdlp_leftovers(out_dir, job["id"])
        raise RuntimeError("yt-dlp 下载视频失败: " + " / ".join(tail))

    # 只接受「真的有视频轨」的容器；音频分片/合并失败残留一律不算
    found = [p for p in out_dir.glob(f"{job['id']}.*")
             if p.suffix.lower() in _VIDEO_CONTAINERS]
    found = [p for p in found if _has_video_stream(p)]
    if not found:
        _cleanup_ytdlp_leftovers(out_dir, job["id"])
        raise RuntimeError(
            "yt-dlp 未产出可用视频（可能视频 CDN 不可达）: " + " / ".join(tail))
    _cleanup_ytdlp_leftovers(out_dir, job["id"], keep=set(found))
    return max(found, key=lambda p: p.stat().st_size)


def _cleanup_ytdlp_leftovers(out_dir: Path, job_id: str, keep=None) -> None:
    """清理 yt-dlp 的分片/半成品（``<job>.<format>.m4a`` 等），避免堆积。"""
    keep = {str(p) for p in (keep or set())}
    for p in out_dir.glob(f"{job_id}.*"):
        if str(p) in keep:
            continue
        try:
            p.unlink()
        except OSError:
            pass


#: 视为「视频容器」的扩展名（音频分片如 .m4a / .mp3 不算）
_VIDEO_CONTAINERS = {".mp4", ".mkv", ".flv", ".webm", ".ts", ".mov", ".avi"}


def _has_video_stream(path: Path) -> bool:
    """用 ffprobe/ffmpeg 确认文件含视频轨（无 ffprobe 时按容器判定）。"""
    from app.ffmpeg_util import probe

    try:
        info = probe(path)
    except Exception:  # noqa: BLE001
        return True  # 探测失败则不阻拦（交给上层转封装报错）
    streams = info.get("streams") or []
    if not streams:
        return True
    return any(s.get("codec_type") == "video" for s in streams)


def _download_external(
    name: str,
    exe: str,
    job: dict,
    out_dir: Path,
    tasks: TaskManager | None,
    task_id: str | None,
) -> Path:
    """用外部下载器（BBDown / yutto 等，见 _ENGINES 注册表）下载完整视频。"""
    if tasks and task_id:
        tasks.update(task_id, message=f"下载完整视频（{name}）…")

    eng = next((e for e in _ENGINES if e["name"] == name), None)
    if eng is None:
        raise RuntimeError(f"未知下载引擎: {name}")
    cmd = eng["build_args"](exe, job, out_dir, sessdata())

    before = {p for p in out_dir.rglob("*") if p.is_file()}
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    after = [p for p in out_dir.rglob("*")
             if p.is_file() and p not in before
             and p.suffix.lower() in (".mp4", ".mkv", ".flv", ".webm", ".ts", ".mov")]
    if not after:
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            raise RuntimeError(f"{name} 下载失败: " + " / ".join(tail))
        raise RuntimeError(f"{name} 未产出视频文件")
    if tasks and task_id and tasks.cancelled(task_id):
        raise TaskCancelled()
    return max(after, key=lambda p: p.stat().st_size)


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

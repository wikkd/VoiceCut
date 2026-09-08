"""B 站直链代理（方案 A：免登录 480p 单文件 MP4）。

- yt-dlp 解析出可直接播放的 单文件 MP4 直链
- 后端对该 URL 做 Range 代理（带 Referer 头），浏览器 <video> 可拖动
- 同时后台整文件下载到临时目录 → 提取音频 → 成为可剪辑素材

限制: 免登录通常只有 360p/480p；720p+ 需 SESSDATA cookie（后续扩展）。
"""
from __future__ import annotations

from pathlib import Path


def resolve_formats(url: str) -> dict:
    """用 yt-dlp 解析视频信息，挑选最佳 单文件MP4 直链。

    返回 {"title", "formats": [...], "pick": {...}}。
    需要网络；失败抛 RuntimeError。
    """
    import yt_dlp  # 延迟导入

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"yt-dlp 解析失败: {exc}") from exc

    formats = info.get("formats") or []
    # 单文件 MP4 = 同时含视频+音频
    single = [f for f in formats
              if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
              and (f.get("ext") == "mp4" or f.get("protocol", "").startswith("http"))]
    pick = None
    if single:
        pick = max(single, key=lambda f: f.get("height") or 0)

    return {
        "title": info.get("title", ""),
        "duration": info.get("duration"),
        "id": info.get("id"),
        "formats": [{"format_id": f.get("format_id"), "ext": f.get("ext"),
                     "height": f.get("height"), "vcodec": f.get("vcodec"),
                     "acodec": f.get("acodec"), "url": f.get("url", "")[:120]}
                    for f in formats][:30],
        "pick": pick and {k: pick.get(k) for k in ("format_id", "ext", "height", "url")},
    }


def _referer_for(url: str) -> str:
    """B 站 CDN 防盗链需要 Referer。"""
    return "https://www.bilibili.com/"


class StreamProxy:
    """B 站直链 Range 代理 + 边播边缓存。下一阶段实现完整 HTTP 端点。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._ref = _referer_for

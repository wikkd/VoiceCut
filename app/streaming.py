"""HTTP Range 流式响应助手：为 /api/audio /api/video 提供 206 分段流，支持浏览器 seek。"""
from __future__ import annotations

from pathlib import Path

from flask import Response, request

CHUNK = 256 * 1024

_MIME = {
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}


def mime_for(path: Path) -> str:
    return _MIME.get(path.suffix.lower(), "application/octet-stream")


def stream_file(path: str | Path, mimetype: str | None = None) -> Response:
    """把本地文件以支持 Range 的流式响应返回。

    无 Range 头 → 200 全量；有 Range → 206 分段。无效 Range 忽略。
    """
    path = Path(path)
    size = path.stat().st_size
    mimetype = mimetype or mime_for(path)

    start, end, status = 0, size - 1, 200
    headers = {"Accept-Ranges": "bytes", "Content-Type": mimetype}

    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes="):
        try:
            spec = range_header[6:].split(",", 1)[0].strip()
            a, b = spec.split("-", 1)
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:  # 后缀范围: bytes=-N → 最后 N 字节
                n = int(b)
                start = max(0, size - n)
                end = size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(end - start + 1)
        except ValueError:
            start, end, status = 0, size - 1, 200
    if status == 200:
        headers["Content-Length"] = str(size)

    def generate():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = f.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return Response(generate(), status=status, headers=headers)

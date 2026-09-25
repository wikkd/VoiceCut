"""URL 导入（导入即后台下载）单元测试：全部 mock 掉网络，不真实下载。"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import bilibili as bb
from app.config import AppConfig
from app.web import create_app

# ── 下载器探测 / cookie ────────────────────────────────────

def test_bilibili_cookie_from_env(monkeypatch) -> None:
    monkeypatch.delenv("VC_BILIBILI_COOKIE", raising=False)
    monkeypatch.delenv("VC_SESSDATA", raising=False)
    assert bb.bilibili_cookie() == ""
    assert bb.sessdata() == ""

    # 只给 SESSDATA 值 → 自动补键名
    monkeypatch.setenv("VC_SESSDATA", "abc123")
    assert bb.bilibili_cookie() == "SESSDATA=abc123"
    assert bb.sessdata() == "abc123"

    # 给完整 cookie 串
    monkeypatch.setenv("VC_BILIBILI_COOKIE", "SESSDATA=xyz; bili_jct=1")
    assert bb.sessdata() == "xyz"


def test_detect_external_downloader_prefers_explicit(monkeypatch, tmp_path: Path) -> None:
    """显式指定的外部下载器路径优先；不存在则回退 None（用 yt-dlp）。"""
    fake = tmp_path / "BBDown.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("VC_BBDOWN", str(fake))
    monkeypatch.delenv("VC_YUTTO", raising=False)
    assert bb.detect_external_downloader() == ("bbdown", str(fake))
    assert bb.downloader_status()["engine"] == "bbdown"

    monkeypatch.setenv("VC_BBDOWN", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(bb.shutil, "which", lambda *_a, **_k: None)
    assert bb.detect_external_downloader() is None
    assert bb.downloader_status()["engine"] == "yt-dlp"


def test_download_external_bbdown_args(monkeypatch, tmp_path: Path) -> None:
    """BBDown 分支：命令带 --work-dir 与 cookie，产物被识别为视频文件。"""
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        (tmp_path / "out.mp4").write_bytes(b"video")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bb.subprocess, "run", fake_run)
    monkeypatch.setenv("VC_SESSDATA", "tok")
    job = {"id": "j1", "url": "https://b23.tv/x"}
    out = bb._download_external("bbdown", "BBDown.exe", job, tmp_path, None, None)
    assert out.name == "out.mp4"
    assert "--work-dir" in seen["cmd"]
    assert any("SESSDATA=tok" in str(a) for a in seen["cmd"])


# ── 解析与登记分离 ─────────────────────────────────────────

def test_register_job_does_not_resolve(monkeypatch) -> None:
    """register_job 只登记，不做网络解析（保证导入接口立即返回）。"""
    def boom(*_a, **_k):
        raise AssertionError("register_job 不应触发解析")

    monkeypatch.setattr(bb, "resolve_formats", boom)
    job = bb.register_job("https://www.bilibili.com/video/BV1xx")
    assert job["status"] == "registered"
    assert job["video_url"] is None and job["audio_url"] is None
    assert bb.get_job(job["id"])["url"].endswith("BV1xx")


def test_resolve_job_fills_streams(monkeypatch) -> None:
    monkeypatch.setattr(bb, "resolve_formats", lambda url: {
        "title": "标题", "duration": 12.0, "id": "BV1",
        "video": {"format_id": "v", "ext": "mp4", "height": 480, "url": "http://v"},
        "audio": {"format_id": "a", "ext": "m4a", "abr": 132, "url": "http://a"},
    })
    job = bb.resolve_job(bb.register_job("https://www.bilibili.com/video/BV1"))
    assert job["status"] == "resolved"
    assert job["title"] == "标题"
    assert job["video_url"] == "http://v" and job["audio_url"] == "http://a"
    assert job["video_height"] == 480


def test_download_video_prefers_direct_stream(monkeypatch, tmp_path: Path) -> None:
    """有单文件直链时直接下载，不走 yt-dlp CLI / 外部下载器。"""
    monkeypatch.setattr(bb, "detect_external_downloader", lambda: None)

    def fake_download(url, dest, **kwargs):
        Path(dest).write_bytes(b"x" * 10)
        return Path(dest)

    monkeypatch.setattr(bb, "download", fake_download)
    called = {"ytdlp": False}
    monkeypatch.setattr(bb, "_download_with_ytdlp",
                        lambda *a, **k: called.__setitem__("ytdlp", True))

    job = {"id": "j2", "url": "u", "referer": bb.REFERER,
           "video_url": "http://v", "video_ext": "mp4"}
    out = bb.download_video(job, tmp_path)
    assert out.exists() and out.name == "j2.mp4"
    assert called["ytdlp"] is False


def test_download_video_falls_back_when_external_fails(monkeypatch, tmp_path: Path) -> None:
    """外部下载器失败时不应中断导入，应回退到内置路径。"""
    monkeypatch.setattr(bb, "detect_external_downloader", lambda: ("yutto", "yutto.exe"))

    def boom(*_a, **_k):
        raise RuntimeError("yutto 挂了")

    monkeypatch.setattr(bb, "_download_external", boom)
    monkeypatch.setattr(bb, "download",
                        lambda url, dest, **kw: (Path(dest).write_bytes(b"v"), Path(dest))[1])

    job = {"id": "j3", "url": "u", "referer": bb.REFERER,
           "video_url": "http://v", "video_ext": "mp4"}
    out = bb.download_video(job, tmp_path)
    assert out.exists()


def test_ytdlp_failure_does_not_return_audio_fragment(monkeypatch, tmp_path: Path) -> None:
    """回归：视频流下载失败、只剩音频分片时，绝不能把它当成视频返回。

    实测该站视频 CDN 可能连不上而音频正常，旧实现只看「有没有文件」，会返回
    一个只有声音的“预览”。现在必须非零退出即报错，并清掉残留分片。
    """
    def fake_run(cmd, **kwargs):
        # 模拟 yt-dlp：视频流失败、音频成功，留下音频分片，非零退出
        (tmp_path / "vj.f30232.m4a").write_bytes(b"audio-only")
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR: video stream failed")

    monkeypatch.setattr(bb.subprocess, "run", fake_run)
    job = {"id": "vj", "url": "https://b23.tv/x"}
    with pytest.raises(RuntimeError):
        bb._download_with_ytdlp(job, tmp_path, None, None)
    # 残留分片已被清理，避免磁盘堆积
    assert not list(tmp_path.glob("vj.*"))


def test_ytdlp_requires_real_video_stream(monkeypatch, tmp_path: Path) -> None:
    """即使退出码为 0，产物若不含视频轨也应报错。"""
    (tmp_path / "vj.mp4").write_bytes(b"not really")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bb.subprocess, "run", fake_run)
    monkeypatch.setattr(bb, "_has_video_stream", lambda p: False)
    job = {"id": "vj", "url": "https://b23.tv/x"}
    with pytest.raises(RuntimeError):
        bb._download_with_ytdlp(job, tmp_path, None, None)


def test_ytdlp_accepts_real_video(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "vj.mp4").write_bytes(b"video-bytes")
    (tmp_path / "vj.f30232.m4a").write_bytes(b"audio-fragment")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bb.subprocess, "run", fake_run)
    monkeypatch.setattr(bb, "_has_video_stream", lambda p: p.suffix == ".mp4")
    job = {"id": "vj", "url": "https://b23.tv/x"}
    out = bb._download_with_ytdlp(job, tmp_path, None, None)
    assert out.name == "vj.mp4"
    # 音频分片被清理，只保留真正的视频
    assert not (tmp_path / "vj.f30232.m4a").exists()


# ── 端到端（mock 网络）：导入 → 音频素材 + 本地视频预览 ────

def test_url_import_downloads_audio_and_video(tmp_path: Path, sample_video: Path,
                                              monkeypatch) -> None:
    """导入接口立即返回；后台完成音频素材与本地预览，且不再依赖代理流。"""
    from app.web import projects as projects_bp

    app = create_app(AppConfig(workdir=tmp_path))
    client = app.test_client()
    pid = client.post("/api/projects", json={"name": "P"}).get_json()["id"]

    # 解析：给一个「视频直链」= 本地样片（download 被 mock 成复制该文件）
    sample_bytes = sample_video.read_bytes()
    monkeypatch.setattr(bb, "resolve_formats", lambda url: {
        "title": "测试视频", "duration": 3.0, "id": "BV1",
        "video": {"format_id": "v", "ext": "mp4", "height": 480, "url": "http://v"},
        "audio": None,
    })
    monkeypatch.setattr(bb, "detect_external_downloader", lambda: None)

    def fake_download(url, dest, **kwargs):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(sample_bytes)
        return Path(dest)

    monkeypatch.setattr(bb, "download", fake_download)
    # 自动分析链路不在本测试范围
    monkeypatch.setattr(projects_bp, "submit_project_analyze",
                        lambda c, project_id, *, force=False: None)

    r = client.post("/api/url/open", json={"urls": ["https://b23.tv/x"], "project_id": pid})
    assert r.status_code == 200
    res = r.get_json()["results"][0]
    assert res["ok"] is True and res["task_id"]

    # 导入接口不做解析（status 仍是 registered 时才可能这么快返回）
    t = None
    for _ in range(600):
        t = client.get(f"/api/tasks/{res['task_id']}").get_json()
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert t is not None and t["status"] == "done", t

    item = t["result"]["item"]
    assert item["name"] == "测试视频"
    assert item["kind"] == "url"
    # 音频素材可用
    assert client.get(f"/api/audio/{item['id']}").status_code == 200
    # 完整视频已下载为本地预览（不再依赖会过期的代理流）
    assert item["video_url"] == f"/api/video/{item['id']}"
    assert client.get(f"/api/video/{item['id']}").status_code == 200


def test_url_import_survives_video_failure(tmp_path: Path, monkeypatch) -> None:
    """视频下载失败时，音频素材仍应导入成功（尽力而为，不整体失败）。"""
    from app.web import bilibili as wb  # noqa: F401
    from app.web import projects as projects_bp

    app = create_app(AppConfig(workdir=tmp_path))
    client = app.test_client()
    pid = client.post("/api/projects", json={"name": "P"}).get_json()["id"]

    import numpy as np
    import soundfile as sf

    wav_src = tmp_path / "tone.wav"
    sf.write(str(wav_src), (np.sin(np.arange(48000) / 10) * 0.2).astype(np.float32), 48000)

    monkeypatch.setattr(bb, "resolve_formats", lambda url: {
        "title": "只有音频", "duration": 1.0, "id": "BV2",
        "video": None, "audio": {"format_id": "a", "ext": "wav", "abr": 128, "url": "http://a"},
    })
    monkeypatch.setattr(bb, "detect_external_downloader", lambda: None)
    monkeypatch.setattr(bb, "download",
                        lambda url, dest, **kw: (Path(dest).write_bytes(wav_src.read_bytes()),
                                                 Path(dest))[1])
    monkeypatch.setattr(bb, "download_video",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("视频下载失败")))
    monkeypatch.setattr(projects_bp, "submit_project_analyze",
                        lambda c, project_id, *, force=False: None)

    r = client.post("/api/url/open", json={"urls": ["https://b23.tv/y"], "project_id": pid})
    body = r.get_json()["results"][0]
    tid = body["task_id"]
    t = None
    for _ in range(600):
        t = client.get(f"/api/tasks/{tid}").get_json()
        if t["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert t is not None and t["status"] == "done", t
    item = t["result"]["item"]
    # 视频下载失败且无单文件流（DASH）：无本地预览也无 proxy 兜底
    # （proxy 对分离流必然 404，宁缺毋滥）→ video_url 为 None，前端显示占位
    assert client.get(f"/api/video/{item['id']}").status_code == 404
    assert item["video_url"] is None
    assert client.get(f"/api/audio/{item['id']}").status_code == 200


def test_url_open_reports_submit_error(tmp_path: Path) -> None:
    """缺少链接时返回 400（接口契约）。"""
    app = create_app(AppConfig(workdir=tmp_path))
    client = app.test_client()
    r = client.post("/api/url/open", json={"urls": []})
    assert r.status_code == 400


@pytest.mark.parametrize("url", ["https://www.bilibili.com/video/BV1"])
def test_config_exposes_downloader(url: str, tmp_path: Path) -> None:
    app = create_app(AppConfig(workdir=tmp_path))
    cfg = app.test_client().get("/api/config").get_json()
    assert cfg["downloader"]["engine"] in ("yt-dlp", "bbdown", "yutto")


def test_thread_safety_of_job_registry() -> None:
    """并发登记 job 不应丢失（job 表有锁）。"""
    jobs = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        j = bb.register_job(f"https://example.com/{i}")
        with lock:
            jobs.append(j["id"])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(set(jobs)) == 20
    assert all(bb.get_job(j) is not None for j in jobs)

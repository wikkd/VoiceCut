"""E2E: 对运行中的服务做全管线验证（阶段03-06 后端部分）。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8765"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


def post_json(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def post_multipart(path, filepath, filename):
    boundary = "----vc" + uuid.uuid4().hex
    body = b""
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += Path(filepath).read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def wait_task(tid, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, _, body = get(f"/api/tasks/{tid}")
        t = json.loads(body)
        if t["status"] in ("done", "error"):
            return t
        time.sleep(1.0)
    raise TimeoutError(tid)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vc-e2e-"))
    ff = r"D:\ffmpeg\ffmpeg.exe"
    video = tmp / "sample.mp4"
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video)],
                   check=True, capture_output=True)
    print(f"[1] 测试视频: {video} ({video.stat().st_size//1024}KB)")

    # 健康/配置
    st, _, _ = get("/api/health"); print(f"[2] health {st}")
    st, _, cfgb = get("/api/config"); cfg = json.loads(cfgb); print(f"[3] config {st} ffmpeg={cfg['ffmpeg']}")

    # 导入
    st, j = post_multipart("/api/import", video, "sample.mp4")
    print(f"[4] import {st} task={j['task_id']}")
    t = wait_task(j["task_id"]); assert t["status"] == "done", t
    item = t["result"]["item"]
    print(f"[5] import done item={item['id']} name={item['name']} dur={item['duration']} kind={item['kind']}")

    # 峰值
    st, _, peaks = get(item["peaks_url"]); pj = json.loads(peaks)
    print(f"[6] peaks {st} points={len(pj['peaks'])} dur={pj['duration']}")

    # audio Range
    req = urllib.request.Request(BASE + item["audio_url"], headers={"Range": "bytes=0-1023"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"[7] audio Range status={r.status} Content-Range={r.headers.get('Content-Range')} bytes={len(r.read())}")
        assert r.status == 206

    # video Range
    if item["video_url"]:
        req = urllib.request.Request(BASE + item["video_url"], headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[8] video Range status={r.status} bytes={len(r.read())}")
            assert r.status == 206
    else:
        print("[8] 无视频预览")

    # 导出 wav + mp3
    st, ej = post_json("/api/export", {"item_id": item["id"], "start": 0.5, "end": 2.5,
                                       "format": "wav", "sample_rate": 32000})
    print(f"[9] export wav {st} -> {ej['name']} dl={ej['download_url']}")
    st, ej2 = post_json("/api/export", {"item_id": item["id"], "start": 0.5, "end": 2.5,
                                        "format": "mp3", "sample_rate": 44100})
    print(f"[10] export mp3 {st} -> {ej2['name']}")
    st, _, dl = get(ej["download_url"]); print(f"[11] download {st} bytes={len(dl)}")

    # trim
    st, tj = post_json("/api/trim", {"item_id": item["id"]})
    tt = wait_task(tj["task_id"]); assert tt["status"] == "done", tt
    print(f"[12] trim done -> {tt['result']['item']['name']}")

    # denoise
    st, dj = post_json("/api/denoise", {"item_id": item["id"]})
    dt = wait_task(dj["task_id"]); assert dt["status"] == "done", dt
    print(f"[13] denoise done -> {dt['result']['item']['name']}")

    # dataset export (两段: 一个正常, 一个空文本应跳过)
    st, dsj = post_json("/api/dataset/export", {
        "item_id": item["id"], "speaker": "test_speaker", "language": "JP",
        "segments": [
            {"start": 0.5, "end": 2.0, "text": "\u3053\u3093\u306b\u3061\u306f\u4e16\u754c\u3002", "language": "JP", "speaker": "test_speaker"},
            {"start": 2.0, "end": 3.0, "text": "", "language": "JP", "speaker": "test_speaker"},
        ]})
    dst = wait_task(dsj["task_id"]); assert dst["status"] == "done", dst
    res = dst["result"]
    print(f"[14] dataset export count={res['count']} skipped={len(res['skipped'])} out={res['out_dir']}")
    list_txt = Path(res["list_file"]).read_text(encoding="utf-8").strip()
    print(f"      list.txt:\n{list_txt}")
    assert res["count"] == 1 and len(res["skipped"]) == 1

    # transcribe (1 段, medium 已缓存)
    st, trj = post_json("/api/transcribe", {"item_id": item["id"], "model": "medium",
                                            "segments": [{"start": 0.5, "end": 2.5}]})
    trt = wait_task(trj["task_id"], timeout=300); assert trt["status"] == "done", trt
    texts = trt["result"]["texts"]
    print(f"[15] transcribe ok texts={texts!r}")

    print("\n[OK] E2E 全部通过")


if __name__ == "__main__":
    main()

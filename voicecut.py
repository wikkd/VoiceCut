"""VoiceCut 启动入口.

用法:
    .venv\\Scripts\\python.exe voicecut.py [--port 8765] [--no-browser]

启动 Flask 后端并自动打开浏览器访问 http://127.0.0.1:<port>
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

from app.config import AppConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoiceCut — GPT-SoVITS 训练集制作工具")
    parser.add_argument("--port", type=int, default=8765, help="后端端口 (默认 8765)")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = AppConfig()
    print(f"[VoiceCut] 工作目录: {cfg.workdir}")
    print(f"[VoiceCut] ffmpeg   : {cfg.ffmpeg_path}")

    from app.server import create_app

    app = create_app(cfg)

    if not args.no_browser:
        url = f"http://127.0.0.1:{args.port}"
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    print(f"[VoiceCut] 服务已启动: http://127.0.0.1:{args.port}  (Ctrl+C 退出)")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

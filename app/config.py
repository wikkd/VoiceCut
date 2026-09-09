"""全局配置：路径、ffmpeg 探测、常用常量。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AppConfig:
    """应用配置。路径均会自动创建。"""

    # 项目根目录
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    # 运行时工作目录（缓存解码后的 wav / 预览 mp4 / 任务产物）
    workdir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "workdir")

    # 导出默认目录（未指定时用源文件所在目录）
    export_dir: Path | None = None

    # 采样率预设（导出可选）
    sample_rates: tuple = (32000, 44100, 48000)

    # 训练集默认导出参数（GPT-SoVITS 标准）
    dataset_sample_rate: int = 32000
    dataset_channels: int = 1

    def __post_init__(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._ffmpeg: str | None = None

    # ── ffmpeg 探测 ──────────────────────────────────────────
    @property
    def ffmpeg_path(self) -> str:
        if self._ffmpeg is None:
            self._ffmpeg = find_ffmpeg()
        return self._ffmpeg


def find_ffmpeg() -> str:
    """按优先级探测 ffmpeg：环境变量 FFMPEG_PATH → PATH → D 盘已知路径。"""
    candidates: list[str] = []

    env = os.environ.get("FFMPEG_PATH")
    if env:
        candidates.append(env)

    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)

    # 已知本机路径（D 盘环境）
    candidates += [
        r"D:\ffmpeg\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]

    for c in candidates:
        if c and Path(c).exists():
            return str(Path(c).resolve())

    raise RuntimeError(
        "未找到 ffmpeg，请设置环境变量 FFMPEG_PATH 或安装 ffmpeg（例如 D:\\ffmpeg\\ffmpeg.exe）"
    )

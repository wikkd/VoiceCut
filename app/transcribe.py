"""ASR 转写：faster-whisper（日语）。默认 medium，可切 large-v3。

自动适配国内网络：
- huggingface.co 不可达时自动回退到 hf-mirror.com 镜像（可被 VC_HF_ENDPOINT 覆盖）
- 镜像下载禁用 Xet（HF_HUB_DISABLE_XET=1），用普通 HTTP
- 把 torch/lib 加入 PATH 以加载 cublas64_12.dll（faster-whisper GPU 需要）

模型按需下载到 HF 缓存，首次调用耗时；模型实例按 (model, task) 缓存复用。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from app.tasks import TaskManager

_WHISPER_MODELS = {
    "tiny":     "Systran/faster-whisper-tiny",
    "base":     "Systran/faster-whisper-base",
    "small":    "Systran/faster-whisper-small",
    "medium":   "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}

_models: dict[tuple[str, str], object] = {}


def _init_runtime() -> None:
    """一次性运行时准备：torch/lib 进 PATH（提供 cublas64_12.dll 等）。"""
    if getattr(_init_runtime, "_done", False):
        return
    torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
    if torch_lib.exists():
        cur = os.environ.get("PATH", "")
        os.environ["PATH"] = str(torch_lib) + os.pathsep + cur
    _init_runtime._done = True  # type: ignore[attr-defined]


def _load_model(model: str, task: str):
    """加载/缓存 WhisperModel；下载失败自动回退镜像。"""
    key = (model, task)
    cached = _models.get(key)
    if cached is not None:
        return cached

    def _attempt() -> object:
        from faster_whisper import WhisperModel  # 延迟导入
        return WhisperModel(_WHISPER_MODELS[model], device="auto", compute_type="auto")

    try:
        m = _attempt()
    except Exception:
        # 回退镜像（hf-mirror.com）并禁用 Xet
        os.environ["HF_ENDPOINT"] = os.environ.get("VC_HF_ENDPOINT", "https://hf-mirror.com")
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        m = _attempt()
    _models[key] = m
    return m


def transcribe_file(
    wav_path: str | Path,
    *,
    language: str = "ja",
    model: str = "medium",
    task: str = "transcribe",
) -> str:
    """转写单个 WAV，返回规范化文本（保留日文假名/汉字）。"""
    _init_runtime()
    if model not in _WHISPER_MODELS:
        raise ValueError(f"未知模型: {model}，可选 {list(_WHISPER_MODELS)}")

    from faster_whisper import WhisperModel  # noqa: F401 (确保 import 路径一致)

    cached = _load_model(model, task)
    assert cached is not None
    segments, _info = cached.transcribe(str(wav_path), language=language, task=task, vad_filter=True)
    text = "".join(s.text.strip() for s in segments)
    return _normalize_jp_text(text)


def _normalize_jp_text(text: str) -> str:
    """轻度规范：去除 ASR 常见噪声标记。"""
    text = re.sub(r"\[.*?\]", "", text)   # [xx] 标记
    text = text.replace("\u3000", "")     # 全角空格
    return text.strip()


def transcribe_batch(
    clips: list[tuple[str, str]],   # [(wav_path, txt_path), ...]
    *,
    language: str = "ja",
    model: str = "medium",
    task_id: str | None = None,
    tasks: TaskManager | None = None,
) -> list[str]:
    """批量转写并写出 .txt（与 wav 同名）。返回每条文本。"""
    results: list[str] = []
    total = len(clips)
    for i, (wav_path, txt_path) in enumerate(clips):
        if tasks and task_id:
            tasks.update(task_id, progress=i / total if total else 1.0,
                         message=f"转写 {i + 1}/{total}: {Path(wav_path).stem}")
        text = transcribe_file(wav_path, language=language, model=model)
        Path(txt_path).write_text(text + "\n", encoding="utf-8")
        results.append(text)
    if tasks and task_id:
        tasks.update(task_id, progress=1.0, message="转写完成")
    return results


def transcribe_timed(
    wav_path: str | Path,
    *,
    language: str = "ja",
    model: str = "medium",
    task: str = "transcribe",
    progress_cb=None,
) -> list[dict]:
    """Transcribe a whole file with per-segment timestamps (for the subtitle panel)."""
    _init_runtime()
    cached = _load_model(model, task)
    assert cached is not None
    segments, info = cached.transcribe(str(wav_path), language=language, task=task, vad_filter=True)
    total = float(getattr(info, "duration", 0) or 0) or 0.0
    subs: list[dict] = []
    for seg in segments:
        text = _normalize_jp_text(seg.text)
        if text:
            subs.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": text,
            })
        if progress_cb and total > 0:
            progress_cb(min(0.99, float(seg.end) / total))
    if progress_cb:
        progress_cb(1.0)
    return subs

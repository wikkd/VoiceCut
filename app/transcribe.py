"""ASR 转写：faster-whisper（日语）。默认 medium，可切 large-v3。

模型按需下载到 HF 缓存 (默认 ~/.cache/huggingface)，首次运行耗时。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.tasks import TaskManager

_WHISPER_MODELS = {
    "tiny":  "Systran/faster-whisper-tiny",
    "base":  "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def transcribe_file(
    wav_path: str | Path,
    *,
    language: str = "ja",
    model: str = "medium",
    task: str = "transcribe",
) -> str:
    """转写单个 WAV，返回规范化文本（去掉标点噪声，保留日文假名/汉字）。

    模型实例按 model 名缓存复用（首次调用加载模型）。
    """
    from faster_whisper import WhisperModel  # 延迟导入，未装 faster-whisper 时不影响其他功能

    if model not in _WHISPER_MODELS:
        raise ValueError(f"未知模型: {model}，可选 {list(_WHISPER_MODELS)}")

    key = (model, task)
    cached = _models.get(key)
    if cached is None:
        cached = WhisperModel(_WHISPER_MODELS[model], device="auto", compute_type="auto")
        _models[key] = cached

    segments, info = cached.transcribe(str(wav_path), language=language, task=task, vad_filter=True)
    texts = [s.text.strip() for s in segments]
    text = "".join(texts)
    return normalize_jp_text(text)


def _normalize_jp_text(text: str) -> str:
    """轻度规范：保留日文内容，去除常见 ASR 噪声标记。"""
    text = re.sub(r"\[.*?\]", "", text)          # 去除 [xx] 之类的标记
    text = text.replace("\u3000", "")            # 全角空格
    return text.strip()


def transcribe_batch(
    clips: list[tuple[str, str]],  # [(wav_path, text_dst_path), ...]
    *,
    language: str = "ja",
    model: str = "medium",
    task_id: str | None = None,
    tasks: TaskManager | None = None,
) -> list[str]:
    """批量转写并写出 .txt 文件（与 wav 同名）。返回每条的文本。"""
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


_models: dict[tuple[str, str], object] = {}

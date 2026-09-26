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

_models: dict[tuple, object] = {}
_batches: dict[tuple, object] = {}


def _init_runtime() -> None:
    """一次性运行时准备：torch/lib 进 PATH（提供 cublas64_12.dll 等）。"""
    if getattr(_init_runtime, "_done", False):
        return
    torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
    if torch_lib.exists():
        cur = os.environ.get("PATH", "")
        os.environ["PATH"] = str(torch_lib) + os.pathsep + cur
    _init_runtime._done = True  # type: ignore[attr-defined]


def _resolve_runtime() -> tuple[str, str]:
    """显式决定 (device, compute_type)，不再交给 ``"auto"``。

    以前用 ``device="auto", compute_type="auto"``，在 CUDA 上 auto 会解析成
    **int8_float16**（量化档）。实测同一段 120s 音频：

    | 配置                | compute_type  | 加载   | 转写   |
    |---------------------|---------------|--------|--------|
    | auto/auto（旧默认） | int8_float16  | 2353ms | 2.38s  |
    | cuda/float16        | float16       | 1315ms | 2.04s  |

    即显式 float16 又**快 ~15%**、又**加载快 1s**（少一次量化），而且精度更高
    （两者文本仅 1 个字符差异）。CPU 侧用 int8（ctranslate2 的 CPU 推荐档）。

    可用环境变量 ``VC_WHISPER_DEVICE`` / ``VC_WHISPER_COMPUTE`` 覆盖。
    """
    dev = os.environ.get("VC_WHISPER_DEVICE", "").strip()
    ctype = os.environ.get("VC_WHISPER_COMPUTE", "").strip()
    if not dev:
        try:
            import torch
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001  torch 缺失/损坏 -> 退 CPU
            dev = "cpu"
    if not ctype:
        ctype = "float16" if dev.startswith("cuda") else "int8"
    return dev, ctype


def _batch_enabled() -> bool:
    """是否启用 BatchedInferencePipeline（默认关闭，见 :func:`_batched_pipeline`）。"""
    return os.environ.get("VC_WHISPER_BATCH", "").strip() in ("1", "true", "on", "yes")


def _load_model(model: str, task: str):
    """加载/缓存 WhisperModel；下载失败自动回退镜像。"""
    dev, ctype = _resolve_runtime()
    key = (model, task, dev, ctype)
    cached = _models.get(key)
    if cached is not None:
        return cached

    def _attempt() -> object:
        from faster_whisper import WhisperModel  # 延迟导入
        return WhisperModel(_WHISPER_MODELS[model], device=dev, compute_type=ctype)

    try:
        m = _attempt()
    except Exception:
        # 回退镜像（hf-mirror.com）并禁用 Xet
        os.environ["HF_ENDPOINT"] = os.environ.get("VC_HF_ENDPOINT", "https://hf-mirror.com")
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        m = _attempt()
    _models[key] = m
    return m


def _batched_pipeline(model: str, task: str):
    """复用已加载模型构建 BatchedInferencePipeline（按需缓存）。

    ⚠️ **默认关闭**，需 ``VC_WHISPER_BATCH=1``。实测（120s 日语音频，medium）：

    * 速度只有 **1.46~1.51x**（远小于通常宣传的倍数，此处 GPU 本就不慢）；
    * **会改变识别文本** —— 批量管线先按 VAD 分块再合批，每块的上下文与串行
      不同：184 -> 193 字符，且出现个别词识别结果不同
      （「ラブレターが続いた」->「ラブレット付いた」）；段数也从 15 段变成 2 段。

    对做字幕/训练集的场景来说这是**产物质量变化**，不是纯提速，所以不设为默认。
    另注意：段数会变，**绝不能用在 transcribe_timed**（字幕面板依赖逐段边界）。
    """
    from faster_whisper import BatchedInferencePipeline  # 延迟导入

    dev, ctype = _resolve_runtime()
    key = (model, task, dev, ctype)
    pipe = _batches.get(key)
    if pipe is None:
        pipe = BatchedInferencePipeline(model=_load_model(model, task))
        _batches[key] = pipe
    return pipe


def _whisper_call(model: str, task: str, wav_path, *, language: str,
                  word_timestamps: bool, batched_ok: bool):
    """统一的转写入口：返回 ``(segment 生成器, info)``。

    刻意**不**在这里 list() 掉生成器 —— faster-whisper 的转写是惰性的，逐段
    消费才能让调用方的 progress_cb 真正渐进更新。

    ``batched_ok`` 标记该调用是否允许走批量管线（只看文本/词级时间戳的路径可以，
    依赖逐段边界的路径不行）。未启用或不允许时走串行单模型路径。
    """
    cached = _load_model(model, task)
    assert cached is not None
    if batched_ok and _batch_enabled():
        pipe = _batched_pipeline(model, task)
        return pipe.transcribe(
            str(wav_path), language=language, task=task,
            batch_size=int(os.environ.get("VC_WHISPER_BATCH_SIZE", "8")),
            vad_filter=True, word_timestamps=word_timestamps)
    return cached.transcribe(
        str(wav_path), language=language, task=task,
        vad_filter=True, word_timestamps=word_timestamps)


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

    segments, _info = _whisper_call(
        model, task, wav_path, language=language,
        word_timestamps=False, batched_ok=True)
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
    # 绝不走批量管线：BatchedInferencePipeline 按 VAD 分块合批，段数会从 15 段
    # 变成 2 段，而字幕面板依赖逐段的 start/end 边界。
    segments, info = _whisper_call(
        model, task, wav_path, language=language,
        word_timestamps=False, batched_ok=False)
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


def transcribe_clips_full(
    wav_path: str | Path,
    clips: list[tuple[float, float]],
    *,
    language: str = "ja",
    model: str = "medium",
    task: str = "transcribe",
    progress_cb=None,
    cancelled_cb=None,
) -> list[str]:
    """Transcribe the whole file once (word-level) and map words to clips.

    clips: list of (start, end) seconds.
    A word belongs to the clip whose [start, end) contains the word's midpoint.
    Returns a list aligned with ``clips``; clips without words yield "".

    Cancellation: ``cancelled_cb()`` returning True raises TaskCancelled at
    safe checkpoints (between whisper segments / clips).
    """
    from app.tasks import TaskCancelled

    _init_runtime()
    # 只用到词级时间戳（再映射回 clip），不依赖逐段边界，允许走批量管线。
    segments, info = _whisper_call(
        model, task, wav_path, language=language,
        word_timestamps=True, batched_ok=True)

    words: list[tuple[float, float, str]] = []
    total = float(getattr(info, "duration", 0) or 0) or 0.0
    for seg in segments:
        if cancelled_cb and cancelled_cb():
            raise TaskCancelled()
        for w in (seg.words or []):
            try:
                ws, we = float(w.start), float(w.end)
            except (TypeError, ValueError):
                continue
            # faster-whisper 的 Word dataclass 字段是 (start, end, word, probability)，
            # 没有 .text —— 文本在 .word 上
            if w.word and w.word.strip():
                words.append((ws, we, w.word))
        if progress_cb and total > 0:
            progress_cb(min(0.9, float(seg.end) / total))
    if progress_cb:
        progress_cb(0.9)

    texts: list[str] = []
    n = len(clips)
    for i, (cs, ce) in enumerate(clips):
        if cancelled_cb and cancelled_cb():
            raise TaskCancelled()
        parts: list[str] = []
        for ws, we, t in words:
            mid = (ws + we) / 2.0
            if cs <= mid < ce:
                parts.append(t)
        texts.append(_normalize_jp_text("".join(parts)))
        if progress_cb and n:
            progress_cb(min(1.0, 0.9 + 0.1 * (i + 1) / n))
    return texts

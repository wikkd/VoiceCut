"""GPT-SoVITS 训练集导出。

目录结构:
    dataset/
    ├── 001.wav / 001.txt / 002.wav / 002.txt ...
    └── list.txt        # 每行: 绝对路径|speaker|JP|text

规范: 32kHz 单声道 WAV，片段 1~15s（主力 2~8s），自动去头尾静音。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.audio_ops import validate_dataset_clip
from app.ffmpeg_util import export_segment, trim_silence

GPT_SOVITS_LANGS = {"ZH", "EN", "JP", "ZH_EN", "ALL"}


@dataclass
class DatasetSegment:
    """一个训练片段（用户在片段列表里维护的条目）。"""

    start: float
    end: float
    text: str = ""
    language: str = "JP"
    speaker: str = "speaker"
    note: str = ""                      # 主观标记: clean / bgm / reverb
    ok: bool | None = None              # 校验结果（导出时填充）
    issues: list[str] = field(default_factory=list)


def export_dataset(
    src_wav: str | Path,
    segments: list[DatasetSegment],
    out_dir: str | Path,
    *,
    speaker: str = "speaker",
    language: str = "JP",
    sample_rate: int = 32000,
    trim: bool = True,
    export_wav: bool = True,
) -> dict:
    """把片段列表导出为 GPT-SoVITS 标准目录。

    返回 {out_dir, count, skipped, files: [wav相对路径], list_file}。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_wav = Path(src_wav)

    written: list[dict] = []
    skipped: list[dict] = []
    idx = 0

    for seg in segments:
        idx += 1
        base = out_dir / f"{idx:03d}"
        wav_path = base.with_suffix(".wav")
        txt_path = base.with_suffix(".txt")

        text = seg.text.strip()
        if not text:
            skipped.append({"index": idx, "reason": "空文本", "seg": seg})
            continue
        if seg.end - seg.start < 0.8:
            skipped.append({"index": idx, "reason": "片段过短(<0.8s)", "seg": seg})
            continue

        # 1) 切出片段（临时），2) 去头尾静音，3) 重采样 32k 单声道
        tmp = out_dir / f".tmp_{idx:03d}.wav"
        export_segment(src_wav, tmp, seg.start, seg.end, sample_rate=sample_rate)
        final = tmp
        if trim:
            final = out_dir / f".trim_{idx:03d}.wav"
            trim_silence(tmp, final, sample_rate=sample_rate)
        else:
            final = tmp

        # 校验
        check = validate_dataset_clip(final)
        seg.ok = check["ok"]
        seg.issues = check["issues"]

        # 写正式文件
        import shutil

        shutil.move(str(final), str(wav_path))
        txt_path.write_text(text + "\n", encoding="utf-8")

        written.append({
            "index": idx, "wav": wav_path.name, "txt": txt_path.name,
            "start": seg.start, "end": seg.end,
            "duration": check["duration"], "ok": seg.ok, "issues": seg.issues,
        })

    # list.txt: 绝对路径|speaker|language|text
    list_file = out_dir / "list.txt"
    if export_wav:
        lines = []
        for w, seg in zip(written, segments):
            wav_path = out_dir / w["wav"]
            text = (seg.text if seg.text else "").strip().replace("|", " ")
            lang = seg.language or language
            spk = seg.speaker or speaker
            lines.append(f"{wav_path.as_posix()}|{spk}|{lang}|{text}")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "count": len(written),
        "skipped": skipped,
        "files": [w["wav"] for w in written],
        "list_file": str(list_file),
    }

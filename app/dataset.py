"""GPT-SoVITS 训练集导出。

目录结构:
    dataset/
    ├── 001.wav / 001.txt / 002.wav / 002.txt ...
    └── list.txt        # 每行: 绝对路径|speaker|JP|text

规范: 32kHz 单声道 WAV，片段 1~15s（主力 2~8s），
自动去头尾静音 + 响度标准化；空文本 / 过短片段跳过并在返回中标注。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.audio_ops import normalize_loudness, validate_dataset_clip
from app.ffmpeg_util import export_segment, trim_silence


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
    normalize: bool = True,
    min_dur: float = 0.8,
) -> dict:
    """把片段列表导出为 GPT-SoVITS 标准目录。

    返回 {out_dir, count, skipped, files, list_file}。
    skipped 元素: {"index"(源序号), "reason", "seg": {start,end,text}}。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_wav = Path(src_wav)

    written: list[dict] = []
    skipped: list[dict] = []
    num = 0

    for seg in segments:
        text = seg.text.strip()
        if not text:
            skipped.append({"reason": "空文本", "seg": seg})
            continue
        if seg.end - seg.start < min_dur:
            skipped.append({"reason": f"片段过短(<{min_dur:.1f}s)", "seg": seg})
            continue

        num += 1
        wav_path = out_dir / f"{num:03d}.wav"
        txt_path = out_dir / f"{num:03d}.txt"

        # 1) 切片段 → 2) 去头尾静音 → 3) 响度标准化 → 4) 落盘正式文件
        tmp_cut = out_dir / f".tmp_cut_{num:03d}.wav"
        export_segment(src_wav, tmp_cut, seg.start, seg.end, sample_rate=sample_rate)

        tmp_trim = tmp_cut
        if trim:
            tmp_trim = out_dir / f".tmp_trim_{num:03d}.wav"
            trim_silence(tmp_cut, tmp_trim, sample_rate=sample_rate)

        final = tmp_trim
        if normalize:
            tmp_norm = out_dir / f".tmp_norm_{num:03d}.wav"
            normalize_loudness(tmp_trim, tmp_norm, target_db=-16.0)
            final = tmp_norm

        check = validate_dataset_clip(final, min_dur=1.0, max_dur=15.0)
        seg.ok = check["ok"]
        seg.issues = check["issues"]

        shutil.move(str(final), str(wav_path))
        txt_path.write_text(text + "\n", encoding="utf-8")

        written.append({
            "index": num, "wav": wav_path.name, "txt": txt_path.name,
            "start": round(seg.start, 3), "end": round(seg.end, 3),
            "duration": check["duration"], "ok": seg.ok, "issues": seg.issues,
            "text": text, "language": seg.language or language,
            "speaker": seg.speaker or speaker,
        })

    # list.txt: 绝对路径|speaker|language|text （只含已写出的片段，顺序与编号一致）
    list_file = out_dir / "list.txt"
    lines = [
        f"{(out_dir / w['wav']).as_posix()}|{w['speaker']}|{w['language']}|{w['text'].replace('|', ' ')}"
        for w in written
    ]
    list_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "count": len(written),
        "skipped": [{"reason": s["reason"], "seg": {"start": s["seg"].start, "end": s["seg"].end,
                                                     "text": (s["seg"].text or "").strip()}}
                    for s in skipped],
        "files": [w["wav"] for w in written],
        "list_file": str(list_file),
        "list_content": "".join(lines),
    }

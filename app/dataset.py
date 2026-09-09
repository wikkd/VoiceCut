"""GPT-SoVITS training-set exporter.

Directory layouts:
    flat (default):
        dataset/001.wav / 001.txt / ... + list.txt
    per_speaker:
        dataset/<speaker>/train/001.wav ...  +  dataset/<speaker>/val/001.wav ...
        each <speaker>/ gets list.txt (train+val) and val_list.txt (if any)

Spec: 32kHz mono WAV, clips 1~15s (ideal 2~8s), auto trim head/tail silence
+ RMS loudness normalization; empty / too-short clips are skipped and reported.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.audio_ops import normalize_loudness, validate_dataset_clip
from app.ffmpeg_util import export_segment, export_segment_trimmed
from app.tasks import TaskCancelled


@dataclass
class DatasetSegment:
    """One training clip (a row the user maintains in the segment list)."""

    start: float
    end: float
    text: str = ""
    item_id: str = ""
    language: str = "JP"
    speaker: str = "speaker"
    note: str = ""                      # subjective tag: clean / bgm / reverb
    ok: bool | None = None              # validation result (filled on export)
    issues: list[str] = field(default_factory=list)


def _safe_dir(name: str) -> str:
    """Filesystem-safe folder name from a speaker name."""
    out = []
    for ch in name or "":
        out.append("_" if (ch in '/\\:*?"<>|\x00' or ch.isspace()) else ch)
    s = "".join(out).strip("._ ") or "speaker"
    return s[:64]


def export_dataset(
    sources: str | Path | dict[str, str | Path],
    segments: list[DatasetSegment],
    out_dir: str | Path,
    *,
    speaker: str = "speaker",
    language: str = "JP",
    sample_rate: int = 32000,
    trim: bool = True,
    normalize: bool = True,
    min_dur: float = 0.8,
    layout: str = "flat",
    val_ratio: float = 0.0,
    tasks=None,
    task_id: str | None = None,
) -> dict:
    """Export segment lists to a GPT-SoVITS standard directory.

    - layout="flat" (default): out_dir/001.wav ... + list.txt (legacy).
    - layout="per_speaker": out_dir/<speaker>/train|val/001.wav ... with a
      list.txt (+ val_list.txt) per speaker; val clips are picked
      deterministically (every N-th clip per speaker, N=round(1/val_ratio)).
    Unassigned clips (empty speaker) fall back to ``speaker``.

    Returns {out_dir, count, skipped, files, list_file, layout, speakers}.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    per_speaker = layout == "per_speaker"
    val_n = max(1, int(round(1.0 / val_ratio))) if (val_ratio and val_ratio > 0) else 0

    if isinstance(sources, dict):
        def _src(seg: DatasetSegment) -> Path:
            if not seg.item_id:
                raise ValueError("multi-source export requires item_id per segment")
            try:
                return Path(sources[seg.item_id])
            except KeyError:
                raise KeyError("missing source for item %s" % seg.item_id) from None
    else:
        single = Path(sources)

        def _src(seg: DatasetSegment) -> Path:
            return single

    # Group by speaker name (order of first appearance).
    groups: dict[str, list[DatasetSegment]] = {}
    order: list[str] = []
    for seg in segments:
        key = (seg.speaker or speaker) or speaker
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(seg)

    skipped: list[dict] = []
    written: list[dict] = []
    speaker_summaries: list[dict] = []
    total = len(segments)
    done = 0
    counters: dict[str, int] = {}

    for key in order:
        grp = groups[key]
        if per_speaker:
            gdir = out_dir / _safe_dir(key)
            train_dir = gdir / "train"
            val_dir = gdir / "val"
        else:
            gdir = out_dir
            train_dir = out_dir
            val_dir = None

        grp_written: list[dict] = []
        for gi, seg in enumerate(grp):
            done += 1
            if tasks and task_id and tasks.cancelled(task_id):
                raise TaskCancelled()
            if tasks and task_id:
                tasks.update(task_id, progress=done / total if total else 1.0,
                             message=f"dataset export {done}/{total}")
            text = seg.text.strip()
            if not text:
                skipped.append({"reason": "\u7a7a\u6587\u672c", "seg": seg})
                continue
            if seg.end - seg.start < min_dur:
                skipped.append({"reason": f"\u7247\u6bb5\u8fc7\u77ed(<{min_dur:.1f}s)", "seg": seg})
                continue

            is_val = bool(val_dir) and bool(val_n) and ((gi + 1) % val_n == 0)
            target_dir = val_dir if is_val else train_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            cnt = counters.get(str(target_dir), 0) + 1
            counters[str(target_dir)] = cnt
            wav_path = target_dir / f"{cnt:03d}.wav"
            txt_path = target_dir / f"{cnt:03d}.txt"

            tmp_trim = target_dir / f".tmp_cut_{cnt:03d}.wav"
            if trim:
                export_segment_trimmed(_src(seg), tmp_trim, seg.start, seg.end,
                                       sample_rate=sample_rate)
            else:
                export_segment(_src(seg), tmp_trim, seg.start, seg.end, sample_rate=sample_rate)

            final = tmp_trim
            if normalize:
                tmp_norm = target_dir / f".tmp_norm_{cnt:03d}.wav"
                normalize_loudness(tmp_trim, tmp_norm, target_db=-16.0)
                final = tmp_norm

            check = validate_dataset_clip(final, min_dur=1.0, max_dur=15.0)
            seg.ok = check["ok"]
            seg.issues = check["issues"]

            shutil.move(str(final), str(wav_path))
            txt_path.write_text(text + "\n", encoding="utf-8")
            for tmp in target_dir.glob(f".tmp_*_{cnt:03d}.wav"):
                try:
                    tmp.unlink()
                except OSError:
                    pass

            grp_written.append({
                "index": cnt, "wav": wav_path.name, "txt": txt_path.name,
                "rel": wav_path.relative_to(out_dir).as_posix(),
                "path": wav_path, "sub": "val" if is_val else "train",
                "start": round(seg.start, 3), "end": round(seg.end, 3),
                "duration": check["duration"], "ok": seg.ok, "issues": seg.issues,
                "text": text, "language": seg.language or language,
                "speaker": key,
            })

        if not grp_written:
            continue
        written.extend(grp_written)

        def _line(w: dict) -> str:
            return f"{w['path'].as_posix()}|{w['speaker']}|{w['language']}|{w['text'].replace('|', ' ')}"

        if per_speaker:
            list_file = gdir / "list.txt"
            list_file.write_text("\n".join(_line(w) for w in grp_written) + "\n", encoding="utf-8")
            val_entries = [w for w in grp_written if w["sub"] == "val"]
            val_list_file = None
            if val_entries:
                val_list_file = gdir / "val_list.txt"
                val_list_file.write_text("\n".join(_line(w) for w in val_entries) + "\n", encoding="utf-8")
            speaker_summaries.append({
                "name": key, "path": str(gdir),
                "train": sum(1 for w in grp_written if w["sub"] == "train"),
                "val": len(val_entries),
                "list_file": str(list_file),
                "val_list_file": str(val_list_file) if val_list_file else None,
            })
        else:
            speaker_summaries.append({"name": key, "path": str(out_dir),
                                      "train": len(grp_written), "val": 0,
                                      "list_file": str(out_dir / "list.txt"),
                                      "val_list_file": None})

    list_file = out_dir / "list.txt"
    list_lines = [_line(w) for w in written]
    list_file.write_text("\n".join(list_lines) + ("\n" if list_lines else ""), encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "count": len(written),
        "skipped": [{"reason": s["reason"], "seg": {"start": s["seg"].start, "end": s["seg"].end,
                                                     "text": (s["seg"].text or "").strip()}}
                    for s in skipped],
        "files": [w["rel"] for w in written],
        "list_file": str(list_file),
        "list_content": "".join(list_lines),
        "layout": layout,
        "speakers": speaker_summaries,
    }

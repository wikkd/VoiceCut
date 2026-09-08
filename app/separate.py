"""人声分离：demucs htdemucs（--two-stems=vocals）。

产物: 人声轨 vocals.wav + 伴奏轨 no_vocals.wav，两条都进素材列表。
首次运行会自动下载 htdemucs 模型 (~300MB)。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from app.tasks import TaskManager


def demucs_path() -> str:
    return str(Path(sys.executable).parent / "python.exe")


def run_separation(
    wav_path: str | Path,
    out_dir: str | Path,
    *,
    model: str = "htdemucs",
    stems: str = "vocals",
    device: str = "auto",          # auto | cuda | cpu
    task_id: str | None = None,
    tasks: TaskManager | None = None,
) -> dict:
    """运行 demucs 分离，返回 {model_dir, vocals, no_vocals, device}。

    通过解析 stdout 更新任务进度（demucs 输出 "track_i/ total | ..."）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = Path(wav_path)

    cmd = [
        sys.executable, "-m", "demucs.separate",
        "--two-stems", stems,
        "-n", model,
        "-o", str(out_dir),
        "-d", device,
        str(wav_path),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    pattern = re.compile(r"(\d+)\s*/\s*(\d+)\s*\|")
    assert proc.stdout is not None
    for line in proc.stdout:
        if tasks and task_id:
            m = pattern.search(line)
            if m:
                try:
                    tasks.update(task_id, progress=float(m.group(1)) / float(m.group(2)),
                                 message=f"分离中 ({m.group(1)}/{m.group(2)})")
                except ZeroDivisionError:
                    pass
        print(f"[demucs] {line.rstrip()}")
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"demucs 分离失败 (rc={rc})，请查看日志")

    model_dir = out_dir / model / wav_path.stem
    vocals = model_dir / f"{stems}.wav"
    no_vocals = model_dir / f"no_{stems}.wav"
    if not vocals.exists() or not no_vocals.exists():
        raise RuntimeError(f"分离产物缺失: {model_dir}")
    return {"model_dir": str(model_dir), "vocals": str(vocals), "no_vocals": str(no_vocals), "device": device}

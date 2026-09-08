"""环境 + 核心管线冒烟测试（独立于 pytest，便于手工验证）。

用法:
    .\\venv\\Scripts\\python.exe scripts\\smoke_test.py            # 全部阶段
    .\\venv\\Scripts\\python.exe scripts\\smoke_test.py --stage 3  # 只跑第 3 阶段

阶段:
  1 依赖导入
  2 CUDA / GPU (Blackwell sm_120)
  3 ffmpeg 管线 (合成媒体 → 抽流 → 峰值 → 导出 → 质量指标)
  4 noisereduce 降噪
  5 demucs htdemucs GPU 人声分离 (首次运行自动下载模型 ~300MB)
  6 faster-whisper 转写 (tiny 模型验证管线, 首次下载 ~75MB)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import find_ffmpeg  # noqa: E402


def _init_console() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def stage(name: str) -> None:
    print(f"\n=== [{name}] ===================================================", flush=True)


def run(name: str, fn, *args, **kwargs) -> None:
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        print(f"[PASS] {name}  ({time.time() - t0:.1f}s)", flush=True)
        return result
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {exc}", flush=True)
        raise SystemExit(1)


# ── 阶段 1：依赖导入 ─────────────────────────────────────────

def check_imports(tmp: Path) -> None:
    mods = ["flask", "numpy", "scipy", "soundfile", "noisereduce",
            "yt_dlp", "faster_whisper", "torch", "torchaudio", "demucs"]
    import importlib

    for m in mods:
        importlib.import_module(m)
    print(f"  ok: {', '.join(mods)}")


# ── 阶段 2：CUDA ─────────────────────────────────────────────

def check_cuda(tmp: Path) -> None:
    import torch

    print(f"  torch={torch.__version__}  cuda_build={torch.version.cuda}")
    if not torch.cuda.is_available():
        raise RuntimeError("torch.cuda.is_available()=False")
    print(f"  gpu={torch.cuda.get_device_name(0)}  capability={torch.cuda.get_device_capability(0)}")
    a = torch.rand(512, 512, device="cuda")
    torch.matmul(a, a)
    torch.cuda.synchronize()
    print("  gpu matmul ok")


# ── 阶段 3：ffmpeg 管线 ──────────────────────────────────────

def check_ffmpeg_pipeline(tmp: Path) -> None:
    from app.audio_ops import audio_metrics, compute_peaks
    from app.ffmpeg_util import (extract_audio, export_segment, media_duration,
                                 probe, trim_silence)

    ff = find_ffmpeg()
    print(f"  ffmpeg={ff}")

    # 合成 3s 测试视频
    video = tmp / "test.mp4"
    subprocess.run(
        [ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video)],
        check=True, capture_output=True,
    )
    info = probe(video)
    dur = media_duration(video)
    print(f"  video duration={dur:.2f}s  format={info['format']['format_name']}")

    # 抽音频
    wav = extract_audio(video, tmp / "audio.wav", sample_rate=48000)
    print(f"  extracted={wav.name} duration={media_duration(wav):.2f}s")

    # 波形峰值
    peaks = compute_peaks(wav, max_points=4000)
    print(f"  peaks={len(peaks)} points, min={min(p[0] for p in peaks):.2f}, max={max(p[1] for p in peaks):.2f}")

    # 导出选区
    cut = export_segment(wav, tmp / "cut.wav", 0.5, 1.75, sample_rate=32000)
    print(f"  export cut.wav duration={media_duration(cut):.2f}s (expect ~1.25s)")

    # 质量指标
    m = audio_metrics(wav)
    print(f"  metrics rms_db={m['rms_db']} peak_db={m['peak_db']} "
          f"silence={m['silence_ratio']} clipping={m['clipping']}")

    # 去静音（3s 纯音 → 几乎不变，验证命令可执行）
    trimmed = trim_silence(wav, tmp / "trimmed.wav", sample_rate=48000)
    print(f"  trim_silence ok (duration={media_duration(trimmed):.2f}s)")


# ── 阶段 4：noisereduce ──────────────────────────────────────

def check_denoise(tmp: Path) -> None:
    import numpy as np
    import soundfile as sf

    from app.audio_ops import audio_metrics
    from app.denoise import denoise_wav

    rng = np.random.default_rng(42)
    sr = 48000
    t = np.arange(sr) / sr
    clean = 0.5 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    noisy = np.clip(clean + 0.12 * rng.standard_normal(sr).astype(np.float32), -1, 1)
    src = tmp / "noisy.wav"
    sf.write(str(src), noisy, sr, subtype="PCM_16")

    out = denoise_wav(src, tmp / "denoised.wav", stationary=True, prop_decrease=0.9)
    m_before = audio_metrics(src)
    m_after = audio_metrics(out)
    print(f"  rms_db: {m_before['rms_db']} → {m_after['rms_db']}  (denoised lower = 噪声被压)")

    # 1s 正弦 + 白噪声，降噪后 RMS 应显著下降
    if m_after["rms_db"] >= m_before["rms_db"] - 0.5:
        raise RuntimeError(f"降噪未见效果: {m_before['rms_db']} → {m_after['rms_db']}")


# ── 阶段 5：demucs GPU ───────────────────────────────────────

def check_demucs(tmp: Path) -> None:
    import subprocess as sp
    import numpy as np
    import soundfile as sf

    from app.separate import run_separation

    ff = find_ffmpeg()
    src = tmp / "mix.wav"
    # 2s 混合音（两个不同频率正弦叠加，模拟有人声成分的音频）
    sp.run([ff, "-y",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
            "-filter_complex", "[0][1]amix=inputs=2:duration=shortest",
            "-ar", "48000", "-ac", "1", str(src)], check=True, capture_output=True)

    out_dir = tmp / "demucs_out"
    res = run_separation(src, out_dir, model="htdemucs", stems="vocals", device="cuda")
    vocals = Path(res["vocals"])
    no_vocals = Path(res["no_vocals"])
    print(f"  vocals={vocals.name} exists={vocals.exists()}")
    print(f"  no_vocals={no_vocals.name} exists={no_vocals.exists()}")
    if not (vocals.exists() and no_vocals.exists()):
        raise RuntimeError("demucs 产物缺失")


# ── 阶段 6：faster-whisper ───────────────────────────────────

def check_whisper(tmp: Path) -> None:
    import numpy as np
    import soundfile as sf

    from app.transcribe import transcribe_file

    sr = 16000
    t = np.arange(sr * 2) / sr
    x = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)   # 非语音音调
    src = tmp / "tone.wav"
    sf.write(str(src), x, sr, subtype="PCM_16")

    text = transcribe_file(src, language="ja", model="tiny")
    print(f"  transcribe ok, text={text!r} (音调无语音, 空文本属正常)")


# ── 主流程 ───────────────────────────────────────────────────

STAGES = {1: check_imports, 2: check_cuda, 3: check_ffmpeg_pipeline,
          4: check_denoise, 5: check_demucs, 6: check_whisper}


def main() -> None:
    parser = argparse.ArgumentParser(description="VoiceCut 环境冒烟测试")
    parser.add_argument("--stage", type=int, choices=sorted(STAGES), help="只跑指定阶段")
    args = parser.parse_args()

    _init_console()
    print(f"VoiceCut smoke test — Python {sys.version.split()[0]}")
    with tempfile.TemporaryDirectory(prefix="vc-smoke-") as td:
        tmp = Path(td)
        stages = [args.stage] if args.stage else sorted(STAGES)
        for n in stages:
            stage(f"阶段 {n}: {STAGES[n].__name__}")
            run(STAGES[n].__name__, STAGES[n], tmp)

    print("\n[OK] 全部冒烟测试通过")


if __name__ == "__main__":
    main()


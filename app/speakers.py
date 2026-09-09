"""Speaker labeling: ECAPA-TDNN embeddings (speechbrain) + hierarchical clustering.

For each whisper timed speech segment we compute a fixed-window speaker embedding,
then cluster embeddings with scipy agglomerative clustering and emit
speaker_segments [{start, end, label}]. If the ECAPA model cannot be loaded /
downloaded, we fall back to MFCC-based clustering so the feature still works
offline (marked quality="mfcc").

Accuracy is approximate by design (over/under-segmentation allowed); the user
corrects via the character pool.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np

TARGET_SR = 16000
_RMS_GATE_DB = -45.0


def _init_runtime() -> None:
    if getattr(_init_runtime, "_done", False):
        return
    torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
    if torch_lib.exists():
        cur = os.environ.get("PATH", "")
        os.environ["PATH"] = str(torch_lib) + os.pathsep + cur
    _init_runtime._done = True  # type: ignore[attr-defined]


_EMBEDDER = None


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _load_embedder():
    """Load (and cache) speechbrain ECAPA embedder; mirror fallback on download fail."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER

    _init_runtime()
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    savedir = str(Path(sys.prefix) / "share" / "voicecut" / "ecapa")
    device = "cuda:0" if _cuda_available() else "cpu"

    def _attempt():
        return EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=savedir,
            run_opts={"device": device},
            local_strategy=LocalStrategy.COPY,  # Windows 无管理员时 symlink 失败，改复制
        )

    try:
        _EMBEDDER = _attempt()
    except Exception:
        os.environ["HF_ENDPOINT"] = os.environ.get("VC_HF_ENDPOINT", "https://hf-mirror.com")
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        _EMBEDDER = _attempt()
    return _EMBEDDER
def read_mono16k(wav_path):
    """Read WAV -> (mono float32 at 16k, 16000)."""
    import soundfile as sf
    import torch
    import torchaudio.functional as F

    wav, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    mono = wav.mean(axis=1)
    if sr != TARGET_SR:
        t = torch.from_numpy(mono.copy()).unsqueeze(0)
        t = F.resample(t, sr, TARGET_SR)
        mono = t.squeeze(0).numpy()
    return mono, TARGET_SR


def _mid_window(seg, sr):
    """Middle ~1s window (or whole segment if shorter) for a stable embedding."""
    win = min(seg.size, sr)
    if seg.size <= win:
        return seg
    start = (seg.size - win) // 2
    return seg[start : start + win]


def _ecapa_embedding(mono, sr, start, end):
    s, e = int(start * sr), min(int(end * sr), mono.size)
    seg = mono[s:e]
    if seg.size < sr // 4:  # < 0.25s -> unreliable
        return None
    if 20.0 * math.log10(float(np.sqrt(np.mean(seg**2)) + 1e-9)) < _RMS_GATE_DB:
        return None
    import torch

    w = _mid_window(seg, sr)
    model = _load_embedder()
    wav_t = torch.from_numpy(np.ascontiguousarray(w, dtype=np.float32)).unsqueeze(0)
    with torch.no_grad():
        emb = model.encode_batch(wav_t).squeeze().cpu().numpy()
    return np.asarray(emb, dtype=np.float64)


def _mfcc_embedding(mono, sr, start, end):
    import torch
    import torchaudio

    s, e = int(start * sr), min(int(end * sr), mono.size)
    seg = mono[s:e]
    if seg.size < sr // 4:
        return None
    if 20.0 * math.log10(float(np.sqrt(np.mean(seg**2)) + 1e-9)) < _RMS_GATE_DB:
        return None
    w = _mid_window(seg, sr)
    mfcc = torchaudio.transforms.MFCC(
        sample_rate=sr, n_mfcc=13,
        melkwargs={"n_fft": 400, "hop_length": 160, "n_mels": 40, "center": True},
    )
    t = torch.from_numpy(np.ascontiguousarray(w, dtype=np.float32)).unsqueeze(0)
    with torch.no_grad():
        m = mfcc(t).squeeze(0)
        emb = m.mean(dim=1).cpu().numpy()
    return np.asarray(emb, dtype=np.float64)
def _cluster_labels(embeddings):
    """Agglomerative clustering -> cluster id per embedding (ordered by tree)."""
    from scipy.cluster.hierarchy import fcluster, linkage

    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [0]
    X = np.stack(embeddings)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    Z = linkage(X, method="average", metric="cosine")
    for thr in (0.42, 0.34, 0.26, 0.18, 0.10):
        lab = fcluster(Z, t=thr, criterion="distance")
        if len(set(lab)) >= 2 or thr == 0.10:
            return [int(x) - 1 for x in lab]
    return [int(x) - 1 for x in fcluster(Z, t=0.42, criterion="distance")]


def generate_speakers(
    wav_path,
    subs,
    *,
    progress_cb=None,
):
    """Label each whisper sub with a speaker label.

    subs: [{start, end, text, ...}] (whisper timed segments).
    Returns {"speaker_segments": [{start,end,label}], "quality": "ecapa"|"mfcc",
             "total": n, "labeled": k, "n_speakers": m}.
    """
    _init_runtime()
    mono, sr = read_mono16k(wav_path)
    total = len(subs)

    quality = "ecapa"
    embeds = []
    try:
        for i, s in enumerate(subs):
            if progress_cb:
                progress_cb(0.1 + 0.7 * i / total if total else 1.0)
            embeds.append(_ecapa_embedding(mono, sr, s["start"], s["end"]))
    except Exception:
        quality = "mfcc"
        embeds = []
        for i, s in enumerate(subs):
            if progress_cb:
                progress_cb(0.1 + 0.7 * i / total if total else 1.0)
            embeds.append(_mfcc_embedding(mono, sr, s["start"], s["end"]))

    valid_idx = [i for i, e in enumerate(embeds) if e is not None]
    valid = [embeds[i] for i in valid_idx]
    clusters = _cluster_labels(valid) if valid else []

    # renumber clusters by first occurrence order
    order = {}
    next_no = 0
    for c in clusters:
        if c not in order:
            order[c] = next_no
            next_no += 1
    n_speakers = next_no

    label_of = {}
    for i, c in zip(valid_idx, clusters):
        label_of[i] = "\u8bf4\u8bdd\u4eba%d" % (order[c] + 1)  # 说话人N

    out = []
    for i, s in enumerate(subs):
        out.append({"start": round(float(s["start"]), 3),
                    "end": round(float(s["end"]), 3),
                    "label": label_of.get(i)})
    if progress_cb:
        progress_cb(1.0)
    return {
        "speaker_segments": out,
        "quality": quality,
        "total": total,
        "labeled": len(valid_idx),
        "n_speakers": n_speakers,
    }
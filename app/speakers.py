"""Speaker labeling: ECAPA-TDNN embeddings (speechbrain) + hierarchical clustering.

For each whisper timed speech segment we compute fixed-window speaker embeddings,
then cluster embeddings with scipy agglomerative clustering and emit
speaker_segments [{start, end, label}]. If the ECAPA model cannot be loaded /
downloaded, we fall back to MFCC-based clustering so the feature still works
offline (marked quality="mfcc").

Windowed sub-segments: a whisper subtitle frequently merges two speakers into one
sentence (typical in anime dialogue). Instead of one embedding per subtitle
(which mixes two voices and pollutes the character voiceprint pool), we slide a
short window inside each subtitle and cluster window embeddings, so a merged
subtitle naturally splits into its two speakers. ``speaker_segments`` is emitted
at window-run granularity (finer than subtitles); ``dominant_label`` binds a time
range to its dominant speaker and flags ``mixed`` ranges that really contain two
speakers, which are kept unassigned for manual correction.

Accuracy is approximate by design (over/under-segmentation allowed); the user
corrects via the character pool.
"""
from __future__ import annotations

import base64
import math
import os
import sys
import time
import uuid
from pathlib import Path

import numpy as np

TARGET_SR = 16000
_RMS_GATE_DB = -45.0

# Sliding window used to sub-sample each subtitle so a subtitle that mixes two
# speakers is split into clean per-speaker windows instead of one mixed vector.
_WIN = 0.8       # window length (s)
_HOP = 0.4       # window step (s)
_MIN_WIN = 0.25  # ignore trailing windows shorter than this


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
def _window_ranges(start, end):
    """Yield (ws, we) sub-windows covering [start, end); keeps full coverage."""
    start, end = float(start), float(end)
    if end - start < _MIN_WIN:
        return
    t = start
    while t < end - 1e-6:
        we = min(t + _WIN, end)
        if we - t < _MIN_WIN:
            break
        yield (t, we)
        t += _HOP


def dominant_label(start, end, speaker_segments, min_cover=0.35, min_sec=0.25):
    """Dominant speaker label for a time range + ``mixed`` flag.

    The dominant label is the one covering the most time. ``mixed`` is True when
    a second label also covers at least ``min_cover`` of the labeled time and at
    least ``min_sec`` seconds (a real second speaker, not a tiny boundary
    overlap). Returns (label_or_None, mixed_bool).
    """
    cover: dict[str, float] = {}
    for s in speaker_segments:
        lb = s.get("label")
        if not lb:
            continue
        ov = min(float(end), float(s["end"])) - max(float(start), float(s["start"]))
        if ov > 0:
            cover[lb] = cover.get(lb, 0.0) + ov
    if not cover:
        return None, False
    items = sorted(cover.items(), key=lambda kv: -kv[1])
    dom = items[0][0]
    labeled = sum(cover.values())
    if len(items) < 2 or labeled <= 0:
        return dom, False
    second = items[1][1]
    mixed = (second / labeled >= min_cover) and (second >= min_sec)
    return dom, bool(mixed)



def bind_segments(segments, speaker_segments, char_of_label):
    """Rebind a segment list to speaker labels by time overlap.

    Each segment gets ``speakerLabel`` = dominant label. Segments that really
    contain two speakers (``mixed``) keep ``characterId=None`` so they are not
    auto-bound to a single character and surface for manual correction; other
    segments keep an existing characterId or get the matched one. Returns
    (segments, mixed_count).
    """
    out = []
    mixed_count = 0
    for seg in segments:
        seg = dict(seg)
        lb, mixed = dominant_label(
            seg.get("start", 0.0), seg.get("end", 0.0), speaker_segments)
        if lb:
            seg["speakerLabel"] = lb
            seg["mixed"] = bool(mixed)
            if mixed:
                mixed_count += 1
                seg["characterId"] = None
            elif not seg.get("characterId") and lb in char_of_label:
                seg["characterId"] = char_of_label[lb]
        out.append(seg)
    return out, mixed_count


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
    """Label speaker segments with windowed embeddings.

    subs: [{start, end, text, ...}] (whisper timed segments, may be unsorted).
    Each subtitle is sub-sampled into sliding windows; window embeddings are
    clustered and ``speaker_segments`` is emitted at window-run granularity so a
    subtitle that mixes two speakers yields two segments. Returns
    {"speaker_segments": [{start,end,label}], "quality": "ecapa"|"mfcc",
     "total": n, "labeled": k, "mixed": m, "n_speakers": s,
     "label_embeddings": {label: emb}, "sub_labels": [{label,mixed}, ...]}.
    """
    _init_runtime()
    mono, sr = read_mono16k(wav_path)
    total = len(subs)

    def _collect(emb_fn):
        windows = []
        n_total = max(1, total)
        for i, s in enumerate(subs):
            if progress_cb and total:
                progress_cb(0.1 + 0.7 * i / n_total)
            for ws, we in _window_ranges(s["start"], s["end"]):
                emb = emb_fn(mono, sr, ws, we)
                if emb is not None:
                    windows.append((ws, we, emb))
        return windows

    quality = "ecapa"
    windows = []
    try:
        windows = _collect(_ecapa_embedding)
    except Exception:  # noqa: BLE001
        quality = "mfcc"
        windows = _collect(_mfcc_embedding)

    embeds = [w[2] for w in windows]
    clusters = _cluster_labels(embeds) if embeds else []

    # renumber clusters by first occurrence order
    order = {}
    next_no = 0
    for c in clusters:
        if c not in order:
            order[c] = next_no
            next_no += 1
    n_speakers = next_no

    label_of = {}
    for i, c in enumerate(clusters):
        label_of[i] = "\u8bf4\u8bdd\u4eba%d" % (order[c] + 1)  # 说话人N

    # window-run speaker segments (finer than subtitles), in time order
    ordered = sorted(range(len(windows)), key=lambda i: (windows[i][0], windows[i][1]))
    runs = []
    for i in ordered:
        ws, we, _ = windows[i]
        lb = label_of.get(i)
        if lb is None:
            continue
        if runs and runs[-1]["label"] == lb and (ws - runs[-1]["end"]) <= _HOP * 0.75:
            runs[-1]["end"] = max(runs[-1]["end"], we)
        else:
            runs.append({"start": round(float(ws), 3), "end": round(float(we), 3),
                         "label": lb})

    # per-subtitle binding: dominant label + mixed flag
    sub_labels = []
    for s in subs:
        lb, mixed = dominant_label(s["start"], s["end"], runs)
        sub_labels.append({"label": lb, "mixed": bool(mixed)})
    labeled = sum(1 for x in sub_labels if x["label"])
    mixed_count = sum(1 for x in sub_labels if x["mixed"])

    label_embeddings = _label_embeddings(
        list(range(len(clusters))), clusters, label_of, embeds)
    if progress_cb:
        progress_cb(1.0)
    return {
        "speaker_segments": runs,
        "quality": quality,
        "total": total,
        "labeled": labeled,
        "mixed": mixed_count,
        "n_speakers": n_speakers,
        "label_embeddings": label_embeddings,
        "sub_labels": sub_labels,
    }


def _label_embeddings(valid_idx, clusters, label_of, embeds) -> dict:
    """Mean (normalized) representative embedding per detected label."""
    sums: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    for i, c in zip(valid_idx, clusters):
        lb = label_of.get(i)
        e = embeds[i]
        if lb is None or e is None:
            continue
        if lb not in sums:
            sums[lb] = np.asarray(e, dtype=np.float64)
            counts[lb] = 1
        else:
            sums[lb] = sums[lb] + np.asarray(e, dtype=np.float64)
            counts[lb] += 1
    out: dict[str, np.ndarray] = {}
    for lb, s in sums.items():
        mean = s / max(1, counts[lb])
        norm = float(np.linalg.norm(mean)) + 1e-9
        out[lb] = mean / norm
    return out


def embedding_to_b64(arr) -> str:
    a = np.asarray(arr, dtype=np.float32)
    return base64.b64encode(a.tobytes()).decode("ascii")


def embedding_from_b64(raw) -> np.ndarray | None:
    if not raw:
        return None
    try:
        return np.frombuffer(base64.b64decode(raw), dtype=np.float32)
    except Exception:
        return None


def _cos(a, b) -> float:
    na = float(np.linalg.norm(a)) + 1e-9
    nb = float(np.linalg.norm(b)) + 1e-9
    return float(np.dot(a, b) / (na * nb))


def match_labels_to_pool(item_id, label_embeddings, characters, threshold=0.82):
    """Map detected labels of one item onto a project character pool.

    - A label already present as "<item_id>:<label>" on a character is reused.
    - Otherwise the label embedding is matched against characters carrying a
      representative ``embedding``; cosine >= threshold merges (running average).
    - Otherwise a new project character is created.

    Returns (assignments, characters, created):
      assignments: {label: character_id}
      characters : updated pool (new list; caller persists it)
      created    : [character_id, ...] for newly created characters
    """
    from app import project as project_mod
    characters = [dict(c) for c in characters]
    assignments: dict[str, str] = {}
    created: list[str] = []
    for label, emb in sorted((label_embeddings or {}).items()):
        if emb is None:
            continue
        key = f"{item_id}:{label}"
        emb = np.asarray(emb, dtype=np.float32)
        target = None
        for c in characters:
            if key in (c.get("speakerLabels") or []):
                target = c
                break
        if target is None:
            best, best_sim = None, -1.0
            for c in characters:
                ce = embedding_from_b64(c.get("embedding"))
                if ce is None:
                    continue
                sim = _cos(emb, ce)
                if sim > best_sim:
                    best, best_sim = c, sim
            if best is not None and best_sim >= threshold:
                target = best
        if target is not None:
            assignments[label] = target["id"]
            labels = target.get("speakerLabels") or []
            if key not in labels:
                labels.append(key)
            target["speakerLabels"] = labels
            cnt = int(target.get("emb_count") or 1)
            old = embedding_from_b64(target.get("embedding"))
            if old is not None:
                target["embedding"] = embedding_to_b64((old * cnt + emb) / (cnt + 1))
            target["emb_count"] = cnt + 1
        else:
            cid = f"char_{uuid.uuid4().hex[:10]}"
            characters.append({
                "id": cid, "name": label, "color": project_mod.next_color(),
                "speakerLabels": [key], "embedding": embedding_to_b64(emb),
                "emb_count": 1, "created": time.time(),
            })
            assignments[label] = cid
            created.append(cid)
    return assignments, characters, created

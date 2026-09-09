"""Speaker labeling: ECAPA-TDNN embeddings (speechbrain) + hierarchical clustering.

Pipeline (robust against BGM-laden anime audio):
  1. Sliding windows inside each whisper subtitle -> ECAPA embeddings.
  2. AVERAGE the windows per subtitle into one stable subtitle embedding
     (short-window embeddings alone are too noisy: clustering them directly
     used to produce one "speaker" per window, e.g. 999 roles for one video).
  3. Cluster the subtitle embeddings with an adaptive cosine cutoff -> a sane
     number of speakers (~3-15 by default).
  4. Vote each subtitle's windows against the speaker centroids: clean
     subtitles yield one speaker segment; subtitles that really mix two
     speakers are split at window level and flagged ``mixed`` (kept unassigned
     for manual correction via the character pool).

If ECAPA cannot be loaded/downloaded we fall back to MFCC-based clustering so
the feature still works offline (marked quality="mfcc").  Accuracy is
approximate by design (over/under-segmentation allowed); the user corrects via
the character pool.
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

# Sliding window used to sub-sample each subtitle.  The per-subtitle windows are
# then AVERAGED into one stable subtitle embedding before clustering (short
# 0.8s windows are too noisy on BGM-laden anime audio and used to fragment the
# pool into one "speaker" per window).  1.4s / 0.5s hop keeps full coverage of
# each subtitle while giving ECAPA enough speech to be discriminative.
_WIN = 1.4       # window length (s)
_HOP = 0.5       # window step (s)
_MIN_WIN = 0.3   # ignore trailing windows shorter than this

# Agglomerative clustering cutoff (cosine distance, average linkage) used on the
# stable subtitle-level embeddings, with adaptive adjustment into a sane band so
# we never regress to "one speaker per window" (hundreds of labels) nor merge an
# entire video into a single speaker.
_CLUSTER_THR = 0.60  # cosine distance cutoff
_CLUSTER_MIN_K = 3   # tighten cutoff (finer) if fewer clusters than this
_CLUSTER_MAX_K = 15  # loosen cutoff (coarser) if more clusters than this


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


def _cluster_labels(embeddings, thr=_CLUSTER_THR, min_k=_CLUSTER_MIN_K,
                  max_k=_CLUSTER_MAX_K):
    """Agglomerative clustering (average linkage, cosine) -> cluster id per embedding.

    Cut the dendrogram at ``thr`` cosine distance.  Because anime window /
    subtitle embeddings vary a lot, the count is adjusted adaptively: if the
    cutoff over-merges (< ``min_k`` clusters) we tighten it; if it over-
    fragments (> ``max_k`` clusters, e.g. one speaker per window) we loosen it.
    Returns arbitrary cluster ids; callers renumber by first appearance.
    """
    from scipy.cluster.hierarchy import fcluster, linkage

    n = len(embeddings)
    if n == 0:
        return []
    if n == 1:
        return [0]
    X = np.stack(embeddings)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    Z = linkage(X, method="average", metric="cosine")

    def _cut(t: float):
        return [int(x) - 1 for x in fcluster(Z, t=t, criterion="distance")]

    lab = _cut(thr)
    k = len(set(lab))
    if k < min_k:
        for t in (0.55, 0.50, 0.45, 0.40, 0.35, 0.30):
            lab = _cut(t)
            if len(set(lab)) >= min_k:
                return lab
    elif k > max_k:
        for t in (0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
            lab = _cut(t)
            if len(set(lab)) == 1:
                break
            if 2 <= len(set(lab)) <= max_k:
                return lab
    return lab


def stale_characters(item_id: str, characters: list) -> list:
    """Auto-created garbage characters from previous runs of ``item_id``.

    Old versions clustered per-window ECAPA embeddings, producing hundreds of
    "说话人N" roles (one per window).  Re-running recognition would otherwise
    keep those stale roles in the pool.  A character is safe to drop when it is
    auto-named (说话人N), only ever attached to this item's labels, never merged
    / reused (emb_count <= 1) and not trained (no ``exp``).  Returns the list of
    characters that should be removed before re-binding.
    """
    prefix = f"{item_id}:"
    out = []
    for c in characters:
        if c.get("exp"):
            continue  # trained model attached -> keep
        labels = c.get("speakerLabels") or []
        if not labels:
            continue
        if not c.get("name", "").startswith("\u8bf4\u8bdd\u4eba"):
            continue
        if int(c.get("emb_count") or 1) > 1:
            continue
        if all(lb.startswith(prefix) for lb in labels):
            out.append(c)
    return out


def generate_speakers(
    wav_path,
    subs,
    *,
    progress_cb=None,
):
    """Label speaker segments from whisper subtitles.

    Robust two-level pipeline that avoids both under- and over-fragmentation:

      1. compute short sliding-window ECAPA embeddings inside each subtitle,
      2. AVERAGE the windows per subtitle into one stable subtitle embedding
         (short-window ECAPA on BGM-laden anime audio is too noisy to cluster
         directly -- it used to produce one label per window, e.g. 999 roles
         for a single video),
      3. cluster the stable subtitle embeddings with an adaptive cosine cutoff
         -> a sane number of speakers (default ~3-15),
      4. vote each subtitle's windows against the speaker centroids: a clean
         subtitle yields one speaker segment; a genuinely two-speaker subtitle
         (whisper often merges a dialogue line) is split at window level and
         flagged ``mixed`` so it stays unassigned for manual correction.

    Returns {"speaker_segments": [{start,end,label}], "quality", "total",
             "labeled", "mixed", "n_speakers", "label_embeddings",
             "sub_labels": [{label,mixed}]}.
    """
    _init_runtime()
    mono, sr = read_mono16k(wav_path)
    total = len(subs)

    def _collect(emb_fn):
        win = []
        for i, s in enumerate(subs):
            if progress_cb and total:
                progress_cb(0.1 + 0.7 * i / max(1, total))
            for ws, we in _window_ranges(s["start"], s["end"]):
                emb = emb_fn(mono, sr, ws, we)
                if emb is not None:
                    win.append((i, ws, we, emb))
        return win

    quality = "ecapa"
    windows: list = []
    try:
        windows = _collect(_ecapa_embedding)
    except Exception:  # noqa: BLE001
        quality = "mfcc"
        windows = _collect(_mfcc_embedding)

    # subtitle-level averaged embeddings (stable) -> cluster speakers;
    # ``acc`` keeps (ws, we, emb) per subtitle for later window voting.
    acc: dict[int, list] = {}
    for i, ws, we, e in windows:
        acc.setdefault(i, []).append((ws, we, e))
    sub_embs = [None] * total
    for i, vecs in acc.items():
        sub_embs[i] = np.mean(np.stack([v[2] for v in vecs]), axis=0)

    valid = [i for i in range(total) if sub_embs[i] is not None]
    clusters = _cluster_labels([sub_embs[i] for i in valid]) if valid else []

    # renumber cluster ids by first appearance (time order)
    order: dict[int, int] = {}
    for j, i in enumerate(sorted(valid, key=lambda i: (subs[i]["start"], subs[i]["end"]))):
        c = clusters[j]
        order.setdefault(c, len(order))
    n_speakers = len(order)
    label_of_sub = {
        i: "\u8bf4\u8bdd\u4eba%d" % (order[c] + 1) for i, c in zip(valid, clusters)
    }

    # speaker centroids (normalized) -> label embeddings for cross-material matching
    label_embeddings: dict[str, np.ndarray] = {}
    for lb, cid in label_of_sub.items():
        m = sub_embs[lb]
        norm = float(np.linalg.norm(m)) + 1e-9
        label_embeddings.setdefault(cid, np.zeros_like(m))
        label_embeddings[cid] = label_embeddings[cid] + m / norm
    for cid in label_embeddings:
        m = label_embeddings[cid]
        label_embeddings[cid] = m / (float(np.linalg.norm(m)) + 1e-9)

    def _nearest_lb(e: np.ndarray) -> str:
        best, best_sim = None, -1.0
        for cid, cemb in label_embeddings.items():
            sim = _cos(e, cemb)
            if sim > best_sim:
                best, best_sim = cid, sim
        return best

    # per-subtitle window votes -> speaker_segments + mixed flag
    speaker_segments: list = []
    sub_labels: list = []
    mixed_count = 0
    for i in range(total):
        s = subs[i]
        cid = label_of_sub.get(i)
        vecs = acc.get(i, [])
        if cid is None or not vecs:
            sub_labels.append({"label": cid, "mixed": False})
            continue
        votes: dict[str, int] = {}
        for _ws, _we, e in vecs:
            nlb = _nearest_lb(e)
            votes[nlb] = votes.get(nlb, 0) + 1
        top = sorted(votes.items(), key=lambda kv: -kv[1])
        dom = top[0][0]
        second = top[1][0] if len(top) > 1 else None
        second_share = (top[1][1] / len(vecs)) if (len(top) > 1 and vecs) else 0.0
        mixed = bool(second is not None and second != dom and len(vecs) >= 2
                     and second_share >= 0.30)
        if mixed:
            # emit window-level runs so downstream dominant_label() sees the
            # real second speaker (kept unassigned for manual correction)
            mixed_count += 1
            runs: list = []
            for ws, we, e in vecs:
                nlb = _nearest_lb(e)
                if runs and runs[-1]["label"] == nlb and (ws - runs[-1]["end"]) <= _HOP * 0.75:
                    runs[-1]["end"] = max(runs[-1]["end"], we)
                else:
                    runs.append({"start": round(float(ws), 3), "end": round(float(we), 3),
                                 "label": nlb})
            speaker_segments.extend(runs)
        else:
            speaker_segments.append({"start": round(float(s["start"]), 3),
                                     "end": round(float(s["end"]), 3), "label": dom})
        sub_labels.append({"label": dom, "mixed": bool(mixed)})

    speaker_segments.sort(key=lambda x: (x["start"], x["end"]))
    labeled = sum(1 for x in sub_labels if x["label"])
    if progress_cb:
        progress_cb(1.0)
    return {
        "speaker_segments": speaker_segments,
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

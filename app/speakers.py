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
_EMBEDDER_NAME = "ecapa"  # 实际加载成功的模型短名（eres2net-voxceleb / ecapa-voxceleb / ...）

# 优先尝试的说话人模型（更强在前；需要联网/缓存下载，失败自动回退到下一个）。
# ERES2Net 对噪声/短视频鲁棒性更好；ECAPA 为既有默认。可用 VC_SPEAKER_MODEL 钉住某个源。
_EMBEDDER_SOURCES = [
    "speechbrain/spkrec-eres2net-voxceleb",
    "speechbrain/spkrec-ecapa-voxceleb",
]


def embedder_name() -> str:
    """实际加载的说话人模型短名（quality 上报用，默认 ecapa）。"""
    return _EMBEDDER_NAME


def _cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _model_cached(savedir: str) -> bool:
    return (Path(savedir) / "hyperparams.yaml").exists()


def _model_savedir(src: str) -> str:
    return str(Path(sys.prefix) / "share" / "voicecut" / src.split("/")[-1])


def _load_embedder():
    """Load (and cache) the best available speechbrain speaker embedder.

    Model choice is offline-friendly and deterministic:

    * ``VC_SPEAKER_MODEL=<org/repo>`` pins one source explicitly (download
      allowed, so this is how you opt into the stronger ERES2Net build).
    * Otherwise only models **already cached** on disk are used, in priority
      order (ERES2Net > ECAPA), so an offline cold start never wastes ~90s in
      huggingface retry loops before falling back.
    * If nothing is cached yet (first run on a networked machine) every source
      is attempted in order, with an hf-mirror retry per source.

    The loaded model's short name is reported by :func:`embedder_name` so the
    ``quality`` string stays truthful.  Raises if every source fails.
    """
    global _EMBEDDER, _EMBEDDER_NAME
    if _EMBEDDER is not None:
        return _EMBEDDER

    _init_runtime()
    from speechbrain.inference.speaker import EncoderClassifier
    from speechbrain.utils.fetching import LocalStrategy

    device = "cuda:0" if _cuda_available() else "cpu"
    pin = os.environ.get("VC_SPEAKER_MODEL", "").strip()
    if pin:
        # 显式指定：允许下载（失败才回退）
        sources = [pin]
    else:
        # 默认只用「已缓存」的模型，避免离线时在下载重试上白等 ~90s；
        # 一个都没缓存（首次运行且联网）才按优先级尝试下载。
        cached = [s for s in _EMBEDDER_SOURCES if _model_cached(_model_savedir(s))]
        sources = cached or list(_EMBEDDER_SOURCES)
    errors: list[tuple[str, Exception]] = []
    for src in sources:
        savedir = _model_savedir(src)

        def _attempt(s=src, d=savedir):
            return EncoderClassifier.from_hparams(
                source=s, savedir=d,
                run_opts={"device": device},
                local_strategy=LocalStrategy.COPY,  # Windows 无管理员时 symlink 失败，改复制
            )

        for attempt in (0, 1):  # 0: 默认源；1: hf-mirror 回退
            try:
                _EMBEDDER = _attempt()
                _EMBEDDER_NAME = src.split("/")[-1]
                return _EMBEDDER
            except Exception as exc:  # noqa: BLE001
                errors.append((src, exc))
                os.environ["HF_ENDPOINT"] = os.environ.get("VC_HF_ENDPOINT", "https://hf-mirror.com")
                os.environ["HF_HUB_DISABLE_XET"] = "1"
    raise RuntimeError(
        "说话人模型加载失败（尝试: %s）: %s"
        % (", ".join(s for s, _ in errors), errors[-1][1])
    )


def read_mono16k(wav_path):
    """Decode WAV -> (mono float32 at 16k, 16000) via ffmpeg.

    Decode+resample in a single streaming pass so the source (e.g. 48kHz)
    is never held in RAM whole; only the 16kHz mono copy is loaded.
    """
    import tempfile
    import uuid as _uuid

    import soundfile as sf

    from app.ffmpeg_util import run_ffmpeg

    tmp = Path(tempfile.gettempdir()) / f"vc_mono16k_{_uuid.uuid4().hex[:8]}.wav"
    try:
        run_ffmpeg([
            "-y", "-i", str(wav_path),
            "-vn", "-ac", "1", "-ar", str(TARGET_SR),
            "-f", "wav", str(tmp),
        ])
        mono, sr = sf.read(str(tmp), dtype="float32", always_2d=False)
    finally:
        tmp.unlink(missing_ok=True)
    mono = np.asarray(mono, dtype=np.float32)
    if mono.ndim == 2:
        mono = mono.mean(axis=1).astype(np.float32)
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


def _silhouette_score(X: np.ndarray, labels) -> float:
    """Mean centroid silhouette of a partition (cosine distance).

    a(i) = distance of sample i to its own cluster centroid,
    b(i) = min distance to the other cluster centroids, s(i) = (b-a)/max(a,b).

    Compared with a raw "intra similarity - inter similarity" score, the
    silhouette properly penalizes merging two similar-but-distinct speakers
    whose samples are sparse: a voice that appears only once is not silently
    swallowed by the nearest big cluster (the old score preferred exactly that
    merge because the merged cluster still looked tight).
    """
    labels = np.asarray(labels)
    uniq = sorted(set(int(x) for x in labels))
    k = len(uniq)
    if k <= 1:
        return -1.0
    centroids: dict[int, np.ndarray] = {}
    for c in uniq:
        m = X[labels == c].mean(axis=0)
        cn = float(np.linalg.norm(m)) + 1e-9
        centroids[c] = m / cn
    scores: list[float] = []
    for i in range(len(X)):
        li = labels[i]
        if int(np.sum(labels == li)) <= 1:
            continue  # singleton clusters have no silhouette
        a = 1.0 - float(X[i] @ centroids[li])
        b = min(1.0 - float(X[i] @ centroids[c]) for c in uniq if c != li)
        scores.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(scores)) if scores else -1.0


def _renumber_by_appearance(labels) -> list:
    """Renumber cluster ids so id 0 is the first cluster to appear."""
    order: dict[int, int] = {}
    out: list = []
    for x in labels:
        if x not in order:
            order[x] = len(order)
        out.append(order[x])
    return out


def _merge_similar_clusters(X: np.ndarray, labels, merge_thr: float = 0.95) -> list:
    """Iteratively merge clusters whose centroids are almost identical.

    Anime voice-lines can legitimately vary a lot, so an adaptive cut sometimes
    splits ONE speaker into two clusters (over-segmentation).  Two clusters
    whose normalized centroids have cosine >= ``merge_thr`` are almost certainly
    the same voice -> merge them (keeping the first-appearing id).

    The default is deliberately high (0.95): measured ECAPA intra-speaker
    consistency on real audio bottoms out around 0.93, so a lower bar would
    merge genuinely distinct-but-similar voices instead of only undoing
    over-segmentation.
    """
    labels = [int(x) for x in labels]
    changed = True
    while changed:
        changed = False
        uniq = sorted(set(labels))
        centroids: dict[int, np.ndarray] = {}
        for c in uniq:
            m = X[np.array(labels) == c].mean(axis=0)
            cn = float(np.linalg.norm(m)) + 1e-9
            centroids[c] = m / cn
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                if float(centroids[a] @ centroids[b]) >= merge_thr:
                    labels = [a if x == b else x for x in labels]
                    changed = True
                    break
            if changed:
                break
    return _renumber_by_appearance(labels)


def _cluster_labels(embeddings, thr=_CLUSTER_THR, min_k=_CLUSTER_MIN_K,
                  max_k=_CLUSTER_MAX_K):
    """Agglomerative clustering (average linkage, cosine) -> cluster id per embedding.

    Instead of cutting the dendrogram at one fixed distance threshold, we scan
    many cutoffs and keep the partition (within ``min_k``..``max_k`` clusters)
    that maximizes separation (intra-cluster similarity - inter-cluster
    similarity).  This adapts to how similar the actual voices are, so we no
    longer hard-code a "0.60 = different speaker" assumption.  Finally clusters
    with near-identical centroids are merged to undo over-segmentation.
    Falls back to the original adaptive-threshold logic when no scan candidate
    fits the desired cluster-count band.
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

    # 1) separation-optimal scan (wide cutoff sweep so very similar voices at
    # cosine ~0.8 can still be separated: their dendrogram merge happens at a
    # distance well below 0.28).  k >= 2 (a single cluster has no separation
    # and is left to the fallback); ``min_k`` is only a preference band, not a
    # hard floor -- a legitimately 2-speaker project must not be force-split.
    best, best_score = None, -1.0
    for t in np.arange(0.04, 0.94, 0.02):
        lab = _cut(float(t))
        k = len(set(lab))
        if k < 2 or k > max_k:
            continue
        score = _silhouette_score(X, lab)
        if score > best_score:
            best_score, best = score, lab
    if best is not None:
        return _merge_similar_clusters(X, best)

    # 2) fallback: original adaptive tightening / loosening
    lab = _cut(thr)
    k = len(set(lab))
    if k < min_k:
        for t in (0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05):
            lab = _cut(t)
            if len(set(lab)) >= min_k:
                return _merge_similar_clusters(X, lab)
    elif k > max_k:
        for t in (0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
            lab = _cut(t)
            if len(set(lab)) == 1:
                break
            if 2 <= len(set(lab)) <= max_k:
                return _merge_similar_clusters(X, lab)
    return _merge_similar_clusters(X, lab)


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


def _aggregate_subtitle_embedding(vecs) -> np.ndarray | None:
    """Robust subtitle-level embedding from its window embeddings.

    Plain averaging lets a single contaminated window (BGM / cross-talk / a
    subtitle that really mixes two speakers) drag the subtitle centroid off,
    which used to split or merge speakers in the clustering stage.  We instead
    keep only the windows most consistent with the bulk of the subtitle
    (trimmed mean, keep 75%) and L2-normalize.  On uniform window vectors this
    degenerates to the plain mean, so existing behaviour is preserved.
    """
    if not vecs:
        return None
    embs = np.stack([v[2] for v in vecs])
    if embs.shape[0] == 1:
        out = embs[0]
    else:
        center = embs.mean(axis=0)
        cn = float(np.linalg.norm(center)) + 1e-9
        center = center / cn
        norms = np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9
        embs_n = embs / norms
        sims = embs_n @ center
        keep = max(1, int(np.ceil(embs.shape[0] * 0.75)))
        idx = np.argsort(-sims)[:keep]
        out = embs[idx].mean(axis=0)
    n = float(np.linalg.norm(out)) + 1e-9
    return out / n


def _collect_source(emb_fn, mono, sr, subs, base, total, progress_cb=None):
    """Compute per-subtitle windows + a stable subtitle-level embedding."""
    acc = {}            # sub_idx -> [(ws, we, emb)]
    sub_embs = [None] * len(subs)
    for i, s in enumerate(subs):
        if progress_cb and total:
            progress_cb(0.1 + 0.7 * (base + i) / max(1, total))
        vecs = []
        for ws, we in _window_ranges(s["start"], s["end"]):
            emb = emb_fn(mono, sr, ws, we)
            if emb is not None:
                vecs.append((ws, we, emb))
        if vecs:
            acc[i] = vecs
            sub_embs[i] = _aggregate_subtitle_embedding(vecs)
    return acc, sub_embs


def generate_speakers_project(sources, *, progress_cb=None):
    """Joint speaker analysis across ALL items of a project.

    ``sources``: list of {"wav_path": str, "subs": [dict]}.  Per-subtitle window
    embeddings are computed and AVERAGED per subtitle, then ALL subtitle
    embeddings across the whole project are clustered TOGETHER into one unified
    speaker set.  Each item's subtitles are assigned to these shared speakers,
    so the same person keeps a single label across every video (previously each
    video was clustered independently and only loosely merged afterwards via a
    similarity threshold, which could both split one person across videos and
    merge two similar voices).

    Returns {"quality", "n_speakers", "label_embeddings": {label: centroid},
             "items": [{speaker_segments, sub_labels, total, labeled, mixed}]}
    where ``items`` is parallel to ``sources``.
    """
    _init_runtime()
    total_all = sum(len(s["subs"]) for s in sources)

    def _collect_all(emb_fn):
        out = []
        base = 0
        for src in sources:
            mono, sr = read_mono16k(src["wav_path"])
            acc, sub_embs = _collect_source(emb_fn, mono, sr, src["subs"], base,
                                            total_all, progress_cb)
            out.append((acc, sub_embs))
            base += len(src["subs"])
        return out

    quality = embedder_name()
    try:
        collected = _collect_all(_ecapa_embedding)
    except Exception:  # noqa: BLE001
        quality = "mfcc"
        collected = _collect_all(_mfcc_embedding)

    # gather every subtitle-level embedding in the project (with its source)
    all_embeds = []
    meta = []  # parallel: (src_idx, sub_idx)
    for si, (_acc, sub_embs) in enumerate(collected):
        for i, e in enumerate(sub_embs):
            if e is not None:
                all_embeds.append(e)
                meta.append((si, i))

    clusters = _cluster_labels(all_embeds) if all_embeds else []

    # renumber cluster ids by first appearance across the project (project order,
    # then subtitle start time), so 说话人1..N are globally consistent
    order: dict[int, int] = {}
    for k in sorted(range(len(meta)),
                    key=lambda k: (meta[k][0], sources[meta[k][0]]["subs"][meta[k][1]]["start"])):
        c = clusters[k]
        order.setdefault(c, len(order))
    n_speakers = len(order)
    label_of_global = ["\u8bf4\u8bdd\u4eba%d" % (order[c] + 1) for c in clusters]

    # unified speaker centroids (normalized) for cross-project matching
    label_embeddings: dict[str, np.ndarray] = {}
    sums: dict[str, list] = {}
    for k, _ in enumerate(meta):
        sums.setdefault(label_of_global[k], []).append(all_embeds[k])
    for lb, vecs in sums.items():
        m = np.mean(np.stack(vecs), axis=0)
        label_embeddings[lb] = m / (float(np.linalg.norm(m)) + 1e-9)

    # per-source subtitle assignment + mixed detection
    items = []
    for si in range(len(sources)):
        acc, _sub_embs = collected[si]
        subs = sources[si]["subs"]
        label_of_sub: dict[int, str] = {}
        for k, (s2, i) in enumerate(meta):
            if s2 == si:
                label_of_sub[i] = label_of_global[k]
        speaker_segments, sub_labels, mixed_count = _assign_source(
            subs, acc, label_of_sub, label_embeddings)
        labeled = sum(1 for x in sub_labels if x["label"])
        items.append({
            "speaker_segments": speaker_segments,
            "sub_labels": sub_labels,
            "total": len(subs), "labeled": labeled, "mixed": mixed_count,
        })

    if progress_cb:
        progress_cb(1.0)
    return {
        "quality": quality,
        "n_speakers": n_speakers,
        "label_embeddings": label_embeddings,
        "items": items,
    }


def _assign_source(subs, acc, label_of_sub, label_embeddings):
    """Vote each subtitle's windows against the shared speaker centroids -> the
    item's speaker_segments + per-subtitle labels (mixed subtitles are split at
    window level and flagged for manual correction)."""
    def _nearest_lb(e: np.ndarray):
        best, best_sim = None, -1.0
        for cid, cemb in label_embeddings.items():
            sim = _cos(e, cemb)
            if sim > best_sim:
                best, best_sim = cid, sim
        return best

    speaker_segments: list = []
    sub_labels: list = []
    mixed_count = 0
    for i in range(len(subs)):
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
    return speaker_segments, sub_labels, mixed_count


def generate_speakers(wav_path, subs, *, progress_cb=None):
    """Single-item convenience wrapper over the joint project analysis."""
    res = generate_speakers_project(
        [{"wav_path": wav_path, "subs": subs}], progress_cb=progress_cb)
    it = res["items"][0]
    return {
        "speaker_segments": it["speaker_segments"],
        "quality": res["quality"],
        "total": it["total"],
        "labeled": it["labeled"],
        "mixed": it["mixed"],
        "n_speakers": res["n_speakers"],
        "label_embeddings": res["label_embeddings"],
        "sub_labels": it["sub_labels"],
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


def match_labels_strong(item_id, label_embeddings, characters,
                        threshold=0.80, margin=0.05):
    """Memory pre-assignment: bind labels to existing pool characters strongly.

    This is the "memory" half of re-identification.  When speaker detection is
    re-run on a project, a voice that already lives in the pool should be
    assigned to the SAME character right away, instead of being re-clustered
    and only weakly matched afterwards (which used to create duplicate
    characters whenever a repeat voice hovered just under the weak-match bar).

    A label is strongly assigned when its best cosine against a pool character
    is >= ``threshold`` AND clearly better than the runner-up (``margin``), so a
    genuinely new voice never gets force-merged into the nearest existing
    character just because the pool has a "least-bad" match.

    Returns (assignments, characters, matched):
      assignments  : {label: character_id} for strongly matched labels only
      characters   : updated pool (new list; caller persists it)
      matched      : list of labels that got a strong assignment
    """
    characters = [dict(c) for c in characters]
    assignments: dict[str, str] = {}
    matched: list[str] = []
    for label, emb in sorted((label_embeddings or {}).items()):
        if emb is None:
            continue
        emb = np.asarray(emb, dtype=np.float32)
        best, best_sim, second = None, -1.0, -1.0
        for c in characters:
            ce = embedding_from_b64(c.get("embedding"))
            if ce is None:
                continue
            sim = _cos(emb, ce)
            if sim > best_sim:
                second = best_sim
                best, best_sim = c, sim
            elif sim > second:
                second = sim
        if best is None or best_sim < threshold or (best_sim - second) < margin:
            continue
        assignments[label] = best["id"]
        key = f"{item_id}:{label}"
        labels = best.get("speakerLabels") or []
        if key not in labels:
            labels.append(key)
        best["speakerLabels"] = labels
        cnt = int(best.get("emb_count") or 1)
        old = embedding_from_b64(best.get("embedding"))
        if old is not None:
            best["embedding"] = embedding_to_b64((old * cnt + emb) / (cnt + 1))
        best["emb_count"] = cnt + 1
        matched.append(label)
    return assignments, characters, matched

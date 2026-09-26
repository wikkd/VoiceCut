"""Speaker labeling: ECAPA-TDNN embeddings (speechbrain) + hierarchical clustering.

Pipeline (robust against BGM-laden anime audio), subtitle-anchored joint model:
  1. Sliding windows INSIDE each whisper subtitle -> ECAPA embeddings. The
     subtitle line is the unit of decision; windows never cross line
     boundaries (字幕锚定).
  2. AVERAGE the windows per subtitle into one stable subtitle embedding
     (trimmed mean, keep 75%) and cluster ALL subtitles of the project
     together into one shared speaker set.
  3. VOTING + VITERBI SMOOTHING over the subtitle sequence (对话连续性先验):
     each line's windows vote for the nearest speaker centroid, then a Viterbi
     pass with a small label-switch cost absorbs single-line noise flips that
     used to split one voice across several "speakers".
  4. MIXED detection by contiguous window runs (not raw vote counts): a line
     really containing two speakers has a CONTIGUOUS run of second-speaker
     windows >= share/duration thresholds; scattered flips (BGM noise) no
     longer trigger false splits. Mixed lines are split at label-change
     boundaries and the TEXT is apportioned at the nearest sentence
     punctuation (字幕推理: never cut mid-word).

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


# read_mono16k 的结果缓存。同一素材在一次任务里会被解码不止一次——例如补扫空白区
# 那段先让 audio_ops.detect_speech_ranges 解码一遍，自己又解码一遍；声纹反馈里
# 每个样本再解码一次。键里带 mtime/size，素材被替换后自动失效；总字节数超预算时
# 按最近最少使用淘汰，避免长驻服务把内存吃掉。
_MONO_CACHE: dict = {}
_MONO_CACHE_ORDER: list = []
_MONO_CACHE_BYTES = 0
_MONO_CACHE_BUDGET = 256 * 1024 * 1024


def _mono_cache_key(wav_path):
    """缓存键：(绝对路径, mtime_ns, size)；取不到 stat（文件不存在）则不缓存。"""
    try:
        p = Path(wav_path)
        st = p.stat()
        return (str(p.resolve()), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def clear_mono_cache() -> None:
    """清空 16k 解码缓存（长任务之间可手动回收内存）。"""
    global _MONO_CACHE_BYTES
    _MONO_CACHE.clear()
    _MONO_CACHE_ORDER.clear()
    _MONO_CACHE_BYTES = 0


def read_mono16k(wav_path):
    """Decode WAV -> (mono float32 at 16k, 16000) via ffmpeg.

    Decode+resample in a single streaming pass so the source (e.g. 48kHz)
    is never held in RAM whole; only the 16kHz mono copy is loaded.

    结果会按 :data:`_MONO_CACHE_BUDGET` 缓存复用。**返回的数组在多个调用方之间
    共享，只读使用，不得就地修改。**
    """
    global _MONO_CACHE_BYTES
    import tempfile
    import uuid as _uuid

    import soundfile as sf

    from app.ffmpeg_util import run_ffmpeg

    key = _mono_cache_key(wav_path)
    if key is not None:
        hit = _MONO_CACHE.get(key)
        if hit is not None:
            _MONO_CACHE_ORDER.remove(key)
            _MONO_CACHE_ORDER.append(key)
            return hit

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

    if key is not None and mono.nbytes <= _MONO_CACHE_BUDGET:
        _MONO_CACHE[key] = (mono, TARGET_SR)
        _MONO_CACHE_ORDER.append(key)
        _MONO_CACHE_BYTES += int(mono.nbytes)
        while _MONO_CACHE_BYTES > _MONO_CACHE_BUDGET and _MONO_CACHE_ORDER:
            old = _MONO_CACHE_ORDER.pop(0)
            prev = _MONO_CACHE.pop(old, None)
            if prev is not None:
                _MONO_CACHE_BYTES -= int(prev[0].nbytes)
    return mono, TARGET_SR


def _mid_window(seg, sr):
    """Middle ~1s window (or whole segment if shorter) for a stable embedding."""
    win = min(seg.size, sr)
    if seg.size <= win:
        return seg
    start = (seg.size - win) // 2
    return seg[start : start + win]


def _window_candidate(mono, sr, start, end):
    """ECAPA/MFCC 共用的前置判定：过短或电平过低返回 None，否则返回中段窗。

    抽成单一实现，是为了让**批量路径与逐条路径走完全相同的门限与取窗逻辑**；
    否则两条路径对"哪些窗有效"的判定会分叉，批量结果就不再等价于逐条结果。
    """
    s, e = int(start * sr), min(int(end * sr), mono.size)
    seg = mono[s:e]
    if seg.size < sr // 4:  # < 0.25s -> unreliable
        return None
    if 20.0 * math.log10(float(np.sqrt(np.mean(seg**2)) + 1e-9)) < _RMS_GATE_DB:
        return None
    return _mid_window(seg, sr)


def _ecapa_embedding(mono, sr, start, end):
    """单窗 ECAPA 嵌入（逐条路径；批量走 :func:`_ecapa_embed_many`）。"""
    import torch

    w = _window_candidate(mono, sr, start, end)
    if w is None:
        return None
    model = _load_embedder()
    wav_t = torch.from_numpy(np.ascontiguousarray(w, dtype=np.float32)).unsqueeze(0)
    with torch.no_grad():
        emb = model.encode_batch(wav_t).squeeze().cpu().numpy()
    return np.asarray(emb, dtype=np.float64)


# 定义时保存一份实现引用。批量路径**只在这个对象**上启用：测试会用 monkeypatch
# 把模块属性 ``_ecapa_embedding`` 换成轻量替身，此时替身不等于这里保存的引用，
# 批量自动关闭 —— 既保住既有测试的语义，也不会误去加载真实模型。
_ECAPA_EMBED_IMPL = _ecapa_embedding


def _mfcc_embedding(mono, sr, start, end):
    import torch
    import torchaudio

    w = _window_candidate(mono, sr, start, end)
    if w is None:
        return None
    mfcc = torchaudio.transforms.MFCC(
        sample_rate=sr, n_mfcc=13,
        melkwargs={"n_fft": 400, "hop_length": 160, "n_mels": 40, "center": True},
    )
    t = torch.from_numpy(np.ascontiguousarray(w, dtype=np.float32)).unsqueeze(0)
    with torch.no_grad():
        m = mfcc(t).squeeze(0)
        emb = m.mean(dim=1).cpu().numpy()
    return np.asarray(emb, dtype=np.float64)


# ── 声纹批量前向 ──────────────────────────────────────────────
# 逐条调 model.encode_batch 时，真正的计算只占一小部分，大头是「提交 + 同步 +
# CPU↔GPU 搬运」的固定开销（实测生产路径 30 ms/条，裸 batch=1 只要 5.8 ms）。
# 把同一素材的中段窗按长度分组、一次前向若干条，等于把 N 次固定开销压成 1 次，
# 干净机器上实测 30.1 ms/条 -> 0.82~1.63 ms/条。
_EMBED_BATCH = 16    # 一次前向的窗数（实测 B=16/32 最优，再大反而退化）
_EMBED_CHUNK = 256   # 一次准备/持有的窗数上限（限制 1s 窗常驻内存 ≈16MB）


def _ecapa_embed_many(windows):
    """一次前向求多条中段窗的 ECAPA 嵌入（按窗长分组，返回 list[ndarray|None]）。

    同一批里等长的窗会 stack 成 (B, T) 一次前向；``_mid_window`` 已把 ≥1s 的窗
    统一截到 sr 长，所以正常素材里绝大多数窗等长，能落进同一个 batch。
    """
    import torch

    model = _load_embedder()
    out: list = [None] * len(windows)
    groups: dict[int, list] = {}
    for i, w in enumerate(windows):
        groups.setdefault(int(w.size), []).append((i, w))
    for _, items in groups.items():
        for k in range(0, len(items), _EMBED_BATCH):
            chunk = items[k:k + _EMBED_BATCH]
            mat = torch.from_numpy(np.stack(
                [np.ascontiguousarray(w, dtype=np.float32) for _, w in chunk]))
            with torch.no_grad():
                embs = model.encode_batch(mat)
            embs = embs.reshape(len(chunk), -1).cpu().numpy()
            for (i, _), e in zip(chunk, embs, strict=False):
                out[i] = np.asarray(e, dtype=np.float64)
    return out


def embed_ranges(emb_fn, mono, sr, ranges):
    """对一组 (start, end) 求声纹，返回等长 list（被门限挡掉的位置为 None）。

    只有 ``emb_fn`` 就是本模块 ECAPA 单窗实现（``_ECAPA_EMBED_IMPL``）时才走批量；
    MFCC 降级路径和测试替身一律逐条调用，语义与历史行为逐位一致。批量整体失败时
    自动退回逐条，保留"单个窗出问题不连累其余窗"的既有容错。

    批量与逐条的数值差异实测为相对 8e-4 / 余弦 0.9999995（同为 GPU 前向，
    批次内归约顺序不同所致），比管线中的判定余量（bind_margin 0.05、
    move_margin 0.08、聚类阈值 0.60 余弦距离）小三个数量级，不改变任何判定。
    """
    ranges = list(ranges)
    if emb_fn is not _ECAPA_EMBED_IMPL:
        return [emb_fn(mono, sr, a, b) for a, b in ranges]
    out: list = [None] * len(ranges)
    try:
        for base in range(0, len(ranges), _EMBED_CHUNK):
            block = ranges[base:base + _EMBED_CHUNK]
            cand = [(base + j, _window_candidate(mono, sr, a, b))
                    for j, (a, b) in enumerate(block)]
            cand = [(i, w) for i, w in cand if w is not None]
            if not cand:
                continue
            for (i, _), e in zip(cand,
                                 _ecapa_embed_many([w for _, w in cand]), strict=False):
                out[i] = e
    except Exception:  # noqa: BLE001  批量路径不可用（显存/OOM/驱动）-> 退回逐条
        return [emb_fn(mono, sr, a, b) for a, b in ranges]
    return out


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



def _split_parts(start, end, speaker_segments):
    """Contiguous (start, end, label) runs of one mixed segment's time span."""
    parts: list = []
    for s in speaker_segments:
        lb = s.get("label")
        if not lb:
            continue
        ps = max(float(start), float(s["start"]))
        pe = min(float(end), float(s["end"]))
        if pe - ps <= 0.01:
            continue
        if parts and parts[-1][2] == lb:
            parts[-1][1] = pe
        else:
            parts.append([ps, pe, lb])
    return parts


def normalize_segments(segments: list, *, edge_gap: float = 0.05,
                       contain_ratio: float = 0.95, max_passes: int = 10) -> tuple[list, int]:
    """按时间排序并去除识别重跑产生的重复/碎片片段（棘轮清理，保守规则）。

    重复判定（同时满足才删，宁少勿多）：
    - 两段时间重叠 ≥ contain_ratio × 较短者；
    - 且起点或终点几乎相同（差 < edge_gap）——识别拆分产生的碎片必然与
      原片段共享一条边界，跨素材的真实对话不会命中。
    保留策略：文本非空者优先；其次保留时间跨度更长者。
    迭代到不再有删除（碎片成链时需多轮）。返回 (cleaned, removed_total)。
    """
    removed_total = 0
    out = [dict(s) for s in segments]
    for _ in range(max_passes):
        out.sort(key=lambda s: (float(s.get("start", 0)), float(s.get("end", 0))))
        kept: list = []
        removed = 0
        for s in out:
            if kept:
                p = kept[-1]
                ov = min(float(p["end"]), float(s["end"])) - max(float(p["start"]), float(s["start"]))
                short = min(float(p["end"]) - float(p["start"]),
                            float(s["end"]) - float(s["start"]))
                same_edge = (abs(float(p["start"]) - float(s["start"])) < edge_gap
                             or abs(float(p["end"]) - float(s["end"])) < edge_gap)
                if short > 0 and same_edge and ov / short >= contain_ratio \
                        and not p.get("locked") and not s.get("locked"):
                    # 人工锁定片段不参与合并：既不被删除，也不吞噬重复段（保守保留两者）
                    p_text = (p.get("text") or "").strip()
                    s_text = (s.get("text") or "").strip()
                    keep_new = (not p_text and bool(s_text)) or \
                               (bool(p_text) == bool(s_text)
                                and float(s["end"]) - float(s["start"])
                                > float(p["end"]) - float(p["start"]))
                    if keep_new:
                        kept[-1] = s
                    removed += 1
                    continue
            kept.append(s)
        out = kept
        removed_total += removed
        if not removed:
            break
    return out, removed_total


def bind_segments(segments, speaker_segments, char_of_label, new_id=None):
    """Rebind a segment list to speaker labels by time overlap.

    Each segment gets ``speakerLabel`` = dominant label. Segments that really
    contain two speakers (``mixed``) are — when ``new_id`` is given — split at
    the label-change boundaries so each part binds its own speaker (the text is
    apportioned by duration ratio; refine wording manually in the segment
    editor). Without ``new_id`` the old behaviour applies: keep them whole with
    ``characterId=None`` for manual correction. Returns (segments, mixed_count).

    防棘轮：拆分产生的子片段带 ``was_split`` 标记；重跑识别时已拆过的片段只
    重绑说话人、不再重切（聚类边界每轮漂移，重切会让片段数逐轮倍增）。
    """
    out = []
    mixed_count = 0
    for seg in segments:
        seg = dict(seg)
        if seg.get("locked"):
            # 人工锁定片段：重跑识别不重绑角色、不拆 mixed、不改任何人工成果
            out.append(seg)
            continue
        lb, mixed = dominant_label(
            seg.get("start", 0.0), seg.get("end", 0.0), speaker_segments)
        if lb:
            seg["speakerLabel"] = lb
            seg["mixed"] = bool(mixed)
        if mixed and not seg.get("was_split"):
            mixed_count += 1
            parts = (_split_parts(seg.get("start", 0.0), seg.get("end", 0.0),
                                  speaker_segments)
                     if new_id is not None else [])
            if len(parts) >= 2:
                text = seg.get("text") or ""
                total = sum(p[1] - p[0] for p in parts)
                pos = 0
                for j, (ps, pe, plb) in enumerate(parts):
                    remain = text[pos:]
                    if j == len(parts) - 1:
                        take = len(remain)
                    elif not remain:
                        take = 0
                    else:
                        # 字幕推理：切点吸附到最近句读，避免从词中间硬切
                        take = min(len(remain),
                                   _apportion_text(remain, (pe - ps) / total))
                    child = {**seg, "id": new_id("s"),
                             "start": round(ps, 3), "end": round(pe, 3),
                             "speakerLabel": plb, "mixed": False,
                             "was_split": True,
                             "characterId": char_of_label.get(plb),
                             "text": text[pos:pos + take]}
                    pos += take
                    out.append(child)
                continue
            seg["characterId"] = None
        elif mixed:
            # 已拆过的片段再次判 mixed：只解除绑定留给人工，不再重切（防棘轮）
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


def _enforce_max_k(X: np.ndarray, labels, max_k: int) -> list:
    """Hard cap on cluster count: merge the most similar centroid pair until k <= max_k.

    The silhouette scan and the fallback can both miss the band when CMS residuals
    (scene-specific BGM after mean removal) scatter far-away singleton directions;
    without this cap a 16-item project once produced 57 "speakers".
    """
    labels = [int(x) for x in labels]
    while len(set(labels)) > max_k:
        uniq = sorted(set(labels))
        cents: dict[int, np.ndarray] = {}
        for c in uniq:
            m = X[np.array(labels) == c].mean(axis=0)
            cn = float(np.linalg.norm(m)) + 1e-9
            cents[c] = m / cn
        best_pair, best_sim = None, -2.0
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                sim = float(cents[a] @ cents[b])
                if sim > best_sim:
                    best_sim, best_pair = sim, (a, b)
        if best_pair is None:
            break
        a, b = best_pair
        labels = [a if x == b else x for x in labels]
    return _renumber_by_appearance(labels)


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
        return _enforce_max_k(X, _merge_similar_clusters(X, best), max_k)

    # 2) fallback: original adaptive tightening / loosening
    lab = _cut(thr)
    k = len(set(lab))
    if k < min_k:
        for t in (0.50, 0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05):
            lab = _cut(t)
            if len(set(lab)) >= min_k:
                return _enforce_max_k(X, _merge_similar_clusters(X, lab), max_k)
    elif k > max_k:
        for t in (0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
            lab = _cut(t)
            if len(set(lab)) == 1:
                break
            if 2 <= len(set(lab)) <= max_k:
                return _enforce_max_k(X, _merge_similar_clusters(X, lab), max_k)
    return _enforce_max_k(X, _merge_similar_clusters(X, lab), max_k)


def orphan_characters(characters: list, existing_item_ids: set,
                      referenced_char_ids: set = frozenset()) -> list:
    """Roles whose labels all point to deleted items.

    Deleting a media item leaves its per-item speaker labels
    (``<item>:<speaker>``) behind; characters holding only such labels can
    never match any segment again and keep polluting the pool ("识别后角色
    没有归一、没有片段").  A character is an orphan when

    * it has at least one label and **every** label's item part is gone,
    * it carries no trained model (no ``exp``),
    * no existing segment still references it via ``characterId``.

    Characters with no labels (manually created) or mixed live/dead labels
    are kept.  Returns the list that should be removed.
    """
    out = []
    for c in characters:
        if c.get("exp"):
            continue
        if c.get("id") in referenced_char_ids:
            continue
        labels = c.get("speakerLabels") or []
        if not labels:
            continue
        if any(lb.split(":", 1)[0] in existing_item_ids for lb in labels):
            continue  # 至少还有一个活素材标签（混合标签也算活）
        out.append(c)
    return out


def segments_from_subs(subs: list) -> list:
    """Build a fresh segment list from subtitle lines.

    Speaker recognition only binds *existing* segments (bind_segments works by
    time overlap); an item that has subtitles but zero segments shows nothing
    in the character pool -- no rows, no audition.  Generating one segment per
    subtitle line (same shape the frontend's addSubToSegments produces) lets
    recognition label them right away.  Only used when the item has no
    segments yet, so manual work is never overwritten.
    """
    segs = []
    for s in subs:
        start = float(s["start"] if not hasattr(s, "start") else s.start)
        end = float(s["end"] if not hasattr(s, "end") else s.end)
        text = (s["text"] if not hasattr(s, "text") else s.text) or ""
        if end <= start:
            continue
        segs.append({"id": None, "start": start, "end": end, "text": text,
                     "language": "JP", "speakerLabel": None,
                     "characterId": None, "mixed": False})
    return segs


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
    """Compute per-subtitle windows + a stable subtitle-level embedding.

    先把本素材的全部窗收集起来做一次批量前向（见 :func:`embed_ranges`），再按
    字幕归组。窗的产出顺序、有效窗集合与进度回调时机都与逐条版本一致。
    """
    acc = {}            # sub_idx -> [(ws, we, emb)]
    sub_embs = [None] * len(subs)
    plan: list = []     # (sub_idx, ws, we)
    for i, sub in enumerate(subs):
        for ws, we in _window_ranges(sub["start"], sub["end"]):
            plan.append((i, ws, we))
    embs = embed_ranges(emb_fn, mono, sr, [(ws, we) for _, ws, we in plan])
    by_sub: dict = {}
    for (i, ws, we), e in zip(plan, embs, strict=False):
        if e is not None:
            by_sub.setdefault(i, []).append((ws, we, e))
    for i in range(len(subs)):
        if progress_cb and total:
            progress_cb(0.1 + 0.7 * (base + i) / max(1, total))
        vecs = by_sub.get(i) or []
        if vecs:
            acc[i] = vecs
            sub_embs[i] = _aggregate_subtitle_embedding(vecs)
    return acc, sub_embs


# ── 字幕推理 + 音色识别联合判定 ──────────────────────────────
_SWITCH_COST = 0.05   # Viterbi 标签切换代价：对话连续性先验强度（吸掉单行噪声翻转）
_MIXED_SHARE = 0.30   # 第二人连续发言占行时长的最小占比（CMS 空间连续段判定）
_MIXED_MIN_SEC = 1.0  # 第二人连续发言的最短绝对时长（≈2 个 1.4s 窗一致，抗散落噪声）


def _nearest_label(e, label_embeddings):
    best, best_sim = None, -1.0
    for lb, cemb in label_embeddings.items():
        sim = _cos(e, cemb)
        if sim > best_sim:
            best, best_sim = lb, sim
    return best


def _window_runs(vecs, label_embeddings):
    """Merge per-window nearest labels into contiguous runs (mixed evidence)."""
    runs: list = []
    for ws, we, e in vecs:
        lb = _nearest_label(e, label_embeddings)
        if runs and runs[-1]["label"] == lb and (ws - runs[-1]["end"]) <= _HOP * 0.75:
            runs[-1]["end"] = max(runs[-1]["end"], we)
        else:
            runs.append({"start": round(float(ws), 3), "end": round(float(we), 3),
                         "label": lb})
    return runs


def _viterbi_labels(line_embs, init_labels, label_embeddings, switch_cost=0.05):
    """Dialogue-continuity smoothing (Viterbi over the subtitle sequence).

    Emission: cosine similarity between the subtitle's stable embedding and
    each shared speaker centroid.  Transition: a small cost when consecutive
    subtitles get different labels — enough to absorb single-line window-noise
    flips (one voice splitting across several "speakers"), while a line with a
    STRONG own voice still switches (its emission gain dwarfs the switch cost).
    Subtitles without an embedding keep their vote label; sequence adjacency
    follows list order (i.e. timeline order within an item).
    Returns labels aligned with the input lists.
    """
    labels = list(label_embeddings.keys())
    out = list(init_labels)
    idxs = [i for i, e in enumerate(line_embs) if e is not None]
    if not labels or len(idxs) < 3:
        return out
    cents = [label_embeddings[lb] for lb in labels]
    k = len(labels)
    m = len(idxs)
    V = [[_cos(line_embs[i], cents[j]) for j in range(k)] for i in idxs]
    back = [[0] * k for _ in range(m)]
    for a in range(1, m):
        for j in range(k):
            best, bj = -1e18, 0
            for pj in range(k):
                sc = V[a - 1][pj] - (switch_cost if pj != j else 0.0)
                if sc > best:
                    best, bj = sc, pj
            V[a][j] += best
            back[a][j] = bj
    j = max(range(k), key=lambda x: V[-1][x])
    path = [j]
    for a in range(m - 1, 0, -1):
        j = back[a][j]
        path.append(j)
    path.reverse()
    for a, i in enumerate(idxs):
        out[i] = labels[path[a]]
    return out


# mixed 拆分时的文本切点：句读标点（字幕推理——绝不在词中间硬切）
_PUNCT_CUT = "。！？…、，,.!?;；:：—～~"


def _apportion_text(text, ratio):
    """Chars to take from ``text`` for a time-ratio ``ratio`` split.

    Prefers cutting right AFTER a sentence punctuation nearest to the raw
    ratio position (within ±25% of the text length); falls back to the raw
    position when no punctuation is near.  Never returns 0 for non-empty text.
    """
    n = len(text)
    if n <= 1:
        return n
    pos = max(1, min(n - 1, int(round(n * ratio))))
    limit = max(2, n // 4)
    for d in range(limit):
        for p in (pos - d, pos + d):
            if 1 <= p <= n - 1 and text[p - 1] in _PUNCT_CUT:
                return p
    return pos


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

    # ── CMS（素材内均值消减）── 番剧的恒定 BGM 床把所有字幕的嵌入同向抬高，
    # 聚类塌缩成"一个说话人吃掉 70-98%"（实测 raw 空间最大簇 72%，CMS 后 31%）。
    # 减去每素材的均值向量消掉常量分量（BGM/声道染色），聚类与窗口投票都在
    # CMS 空间做；角色池匹配用的质心仍回 raw 空间（与存量 embedding b64 兼容）。
    cms_collected = []
    for acc, sub_embs in collected:
        valid = [e for e in sub_embs if e is not None]
        mu = np.mean(np.stack(valid), axis=0) if len(valid) >= 3 else None
        acc_cms = {}
        for i, vecs in acc.items():
            acc_cms[i] = ([(ws_, we_, e_ - mu) for ws_, we_, e_ in vecs]
                          if mu is not None else vecs)
        embs_cms = [None] * len(sub_embs)
        for i, e in enumerate(sub_embs):
            if e is None:
                continue
            if mu is None:
                embs_cms[i] = e
            else:
                v = e - mu
                embs_cms[i] = v / (float(np.linalg.norm(v)) + 1e-9)
        cms_collected.append((acc_cms, embs_cms, mu))

    # gather every subtitle-level embedding in the project (CMS space, with source)
    all_embeds = []
    meta = []  # parallel: (src_idx, sub_idx)
    for si, (_acc, sub_embs, _mu) in enumerate(cms_collected):
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

    # centroids in BOTH spaces: CMS for window voting / Viterbi emissions;
    # raw for cross-project character-pool matching (stored embeddings are raw)
    sums_cms: dict[str, list] = {}
    sums_raw: dict[str, list] = {}
    for k, (si, i) in enumerate(meta):
        lb = label_of_global[k]
        sums_cms.setdefault(lb, []).append(all_embeds[k])
        raw_e = collected[si][1][i]
        if raw_e is not None:
            sums_raw.setdefault(lb, []).append(raw_e)

    def _centroid(vecs):
        m = np.mean(np.stack(vecs), axis=0)
        return m / (float(np.linalg.norm(m)) + 1e-9)

    label_embeddings_cms = {lb: _centroid(v) for lb, v in sums_cms.items()}
    label_embeddings = {lb: _centroid(v) for lb, v in sums_raw.items()}

    # per-source subtitle assignment + mixed detection.
    # 行级标签：Viterbi（字幕级稳定嵌入）；行内 mixed：CMS 空间连续段
    # （raw 空间窗口 argmax 被 BGM 场景起伏扰动，实测 77% 行误判 mixed）。
    items = []
    for si in range(len(sources)):
        acc_cms, sub_embs_cms, _mu = cms_collected[si]
        subs = sources[si]["subs"]
        label_of_sub: dict[int, str] = {}
        for k, (s2, i) in enumerate(meta):
            if s2 == si:
                label_of_sub[i] = label_of_global[k]
        speaker_segments, sub_labels, mixed_count = _assign_source(
            subs, acc_cms, label_of_sub, label_embeddings_cms, sub_embs_cms)
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


def _assign_source(subs, acc, label_of_sub, label_embeddings, sub_embs=None):
    """Joint per-item assignment: window votes -> Viterbi smoothing -> runs.

    Three stages per subtitle line (the unit of decision):
      1. Each window votes for its nearest shared-speaker centroid; contiguous
         same-label windows are merged into runs (evidence for mixed lines).
      2. A Viterbi pass over the line sequence with a small label-switch cost
         (对话连续性先验) re-assigns labels globally — a single line whose
         windows barely flipped to another voice snaps back to its neighbours,
         while a line with a STRONG second voice keeps its own label.
      3. Mixed detection uses run durations (contiguous second-speaker speech
         >= share of the line and >= absolute seconds), not raw vote counts,
         so scattered single-window flips no longer cause false splits.
    """
    n = len(subs)
    line_info = []
    for i in range(n):
        s = subs[i]
        vecs = acc.get(i, [])
        if label_of_sub.get(i) is None or not vecs:
            line_info.append(None)
            continue
        counts: dict[str, int] = {}
        for _ws, _we, e in vecs:
            lb = _nearest_label(e, label_embeddings)
            counts[lb] = counts.get(lb, 0) + 1
        dom = max(counts.items(), key=lambda kv: kv[1])[0]
        line_info.append({"s": s, "vecs": vecs, "dom": dom,
                          "runs": _window_runs(vecs, label_embeddings)})

    # Viterbi smoothing across the line sequence (skips lines w/o embedding)
    embs = [(sub_embs[i] if sub_embs else None) if line_info[i] else None
            for i in range(n)]
    init = [li["dom"] if li else None for li in line_info]
    smoothed = _viterbi_labels(embs, init, label_embeddings, _SWITCH_COST)

    speaker_segments: list = []
    sub_labels: list = []
    mixed_count = 0
    for i, li in enumerate(line_info):
        if li is None:
            sub_labels.append({"label": label_of_sub.get(i), "mixed": False})
            continue
        s = li["s"]
        fin = smoothed[i]
        if fin is None:
            sub_labels.append({"label": None, "mixed": False})
            continue
        dur = max(1e-6, float(s["end"]) - float(s["start"]))
        runs = li["runs"]
        # ── 对话结构门控的 mixed 判定（字幕推理）──
        # 真正的"半句换人"几乎只发生在轮替边界：本行标签 A、相邻行标签 B，
        # 且本行贴着 B 的那一侧连续 ≥1.0s / ≥30% 是 B 的声音。
        # 窗口 argmax 本身噪声大（BGM 场景起伏），中间的散落段不再作为证据。
        sides = []
        if i > 0 and smoothed[i - 1] and smoothed[i - 1] != fin:
            d = 0.0
            for r in runs:
                if r["label"] == smoothed[i - 1]:
                    d += r["end"] - r["start"]
                else:
                    break
            sides.append(("pre", smoothed[i - 1], d))
        if i + 1 < n and smoothed[i + 1] and smoothed[i + 1] != fin:
            d = 0.0
            for r in reversed(runs):
                if r["label"] == smoothed[i + 1]:
                    d += r["end"] - r["start"]
                else:
                    break
            sides.append(("post", smoothed[i + 1], d))
        cand = max((x for x in sides
                    if x[2] >= _MIXED_MIN_SEC and x[2] / dur >= _MIXED_SHARE),
                   key=lambda x: x[2], default=None)
        if cand:
            mixed_count += 1
            side, olb, d = cand
            if side == "pre":
                b0 = float(s["start"]) + d
                speaker_segments.append({"start": round(float(s["start"]), 3),
                                         "end": round(b0, 3), "label": olb})
                speaker_segments.append({"start": round(b0, 3),
                                         "end": round(float(s["end"]), 3),
                                         "label": fin})
            else:
                b0 = float(s["end"]) - d
                speaker_segments.append({"start": round(float(s["start"]), 3),
                                         "end": round(b0, 3), "label": fin})
                speaker_segments.append({"start": round(b0, 3),
                                         "end": round(float(s["end"]), 3),
                                         "label": olb})
            sub_labels.append({"label": fin, "mixed": True})
        else:
            speaker_segments.append({"start": round(float(s["start"]), 3),
                                     "end": round(float(s["end"]), 3),
                                     "label": fin})
            sub_labels.append({"label": fin, "mixed": False})
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


def _pick_embed_fn():
    """优先 ECAPA；模型不可用时降级 MFCC。

    注意：池质心的向量空间由当初识别所用 embedder 决定。正常路径（有 GPU）
    池内质心均为 ECAPA 空间，反馈匹配有效；若池是 MFCC 时代产物，匹配相似度
    会系统性偏低，行为退化为"少匹配、宁少勿多"，不会误绑。
    """
    try:
        _load_embedder()
        return _ecapa_embedding
    except Exception:
        return _mfcc_embedding


def absorb_character_sample(characters: list, character_id: str, emb) -> list:
    """把人工修正片段的声纹作为样本并入角色质心（运行平均，与识别并入一致）。

    返回新池（调用方负责持久化）。角色不存在或样本无效时原样返回。
    """
    characters = [dict(c) for c in characters]
    target = next((c for c in characters if c.get("id") == character_id), None)
    if target is None or emb is None:
        return characters
    emb = np.asarray(emb, dtype=np.float32)
    old = embedding_from_b64(target.get("embedding"))
    cnt = int(target.get("emb_count") or 0)
    if old is None or cnt <= 0:
        target["embedding"] = embedding_to_b64(emb)
        target["emb_count"] = 1
    else:
        target["embedding"] = embedding_to_b64((old * cnt + emb) / (cnt + 1))
        target["emb_count"] = cnt + 1
    return characters


def rescan_assignments(sources: list, characters: list, *, progress_cb=None,
                       cancelled_cb=None, bind_thr: float = 0.80,
                       bind_margin: float = 0.05, move_margin: float = 0.08):
    """人工修正后的静默重匹配：把强化过的角色质心应用到项目内其他片段。

    sources: [{"item_id", "wav_path", "segments": [seg, ...]}]（项目内全部素材）
    规则（宁少勿多；人工 locked 片段与 mixed 混合段永不改动）：
    - 未分配段：best_sim >= bind_thr 且领先第二名 >= bind_margin -> 绑定 best；
    - 已分配段：仅当另一角色以 >= bind_thr 相似且比当前角色质心高出
      >= move_margin 时才改绑（防止单样本并入造成归属来回震荡）。

    返回 (changes, stats)：
      changes = {(item_id, seg_id): character_id}（仅发生变更的段）
      stats   = {"scanned", "bound", "moved", "skipped"}
    """
    emb_fn = _pick_embed_fn()
    cents: list = []
    for c in characters:
        e = embedding_from_b64(c.get("embedding"))
        if e is not None:
            cents.append((c["id"], np.asarray(e, dtype=np.float32)))
    changes: dict = {}
    stats = {"scanned": 0, "bound": 0, "moved": 0, "skipped": 0}
    total = sum(len(s.get("segments") or []) for s in sources) or 1
    done = 0
    for src in sources:
        segs = src.get("segments") or []
        done += len(segs)
        if not src.get("wav_path") or not Path(src["wav_path"]).exists():
            continue
        mono, sr = read_mono16k(src["wav_path"])
        # 本素材所有待判段一次性批量求声纹：逐段单独前向时，固定开销（提交+同步+
        # 搬运）远大于计算量，批量把 N 次开销压成 1 次（实测 20x 量级）。
        todo = [i for i, seg in enumerate(segs)
                if seg.get("id") and not seg.get("locked") and not seg.get("mixed")]
        try:
            embs = embed_ranges(emb_fn, mono, sr,
                                [(float(segs[i].get("start", 0)),
                                  float(segs[i].get("end", 0))) for i in todo])
        except Exception:  # noqa: BLE001  与历史一致：模型不可用则整批视作无嵌入
            embs = [None] * len(todo)
        emb_by_idx = dict(zip(todo, embs, strict=False))
        for si, seg in enumerate(segs):
            if progress_cb:
                progress_cb(min(1.0, done / total))
            if cancelled_cb and cancelled_cb():
                return changes, stats
            sid = seg.get("id")
            if not sid or seg.get("locked") or seg.get("mixed"):
                stats["skipped"] += 1
                continue
            emb = emb_by_idx.get(si)
            if emb is None:
                stats["skipped"] += 1
                continue
            stats["scanned"] += 1
            sims = sorted(((cid, _cos(emb, ce)) for cid, ce in cents),
                          key=lambda x: -x[1])
            if not sims:
                continue
            best_cid, best_sim = sims[0]
            second = sims[1][1] if len(sims) > 1 else -1.0
            cur = seg.get("characterId")
            if not cur:
                if best_sim >= bind_thr and (best_sim - second) >= bind_margin:
                    changes[(src["item_id"], sid)] = best_cid
                    stats["bound"] += 1
            elif cur != best_cid:
                cur_sim = next((s for cid, s in sims if cid == cur), -1.0)
                if best_sim >= bind_thr and (best_sim - cur_sim) >= move_margin:
                    changes[(src["item_id"], sid)] = best_cid
                    stats["moved"] += 1
    return changes, stats

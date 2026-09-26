"""声纹批量前向的**端到端等价性**验收（需要真实 ECAPA 模型）。

单元测试只能覆盖到「桩模型逐位等价 + 前向次数」，证明不了**真实模型下产物是否不变**。
这个脚本补上那一环：同一份素材、同一个模型，跑两遍完整说话人分析 ——

  new = 批量路径（默认）
  old = 逐条路径（把模块属性换成一层 wrapper，使其不等于内部实现引用 -> 自动逐条）

然后逐项比对**决策**（说话人数 / 逐段标签 / mixed 标记 / 质心余弦），而不是只比嵌入数值。

用法:
    .venv/Scripts/python.exe scripts/diag_embed_batch_equiv.py [音频路径]

不带参数时用 workdir/imports 下最大的 .wav。
退出码 0 = 完全等价，1 = 有差异。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app import speakers as S  # noqa: E402

SUB_LEN = 3.5      # 每条字幕时长（模拟 whisper 字幕切分）
SUB_HOP = 4.0


def pick_audio(argv: list[str]) -> Path:
    if len(argv) > 1:
        p = Path(argv[1])
        if not p.exists():
            raise SystemExit(f"音频不存在: {p}")
        return p
    for sub in ("imports", "items", "bilibili"):
        cands = [p for p in (ROOT / "workdir" / sub).glob("*")
                 if p.suffix.lower() in (".wav", ".m4a", ".mp3", ".flac") and p.is_file()]
        if cands:
            return max(cands, key=lambda p: p.stat().st_size)
    raise SystemExit("workdir 下找不到可用音频，请显式传入路径")


def main() -> int:
    src = pick_audio(sys.argv)
    mono, sr = S.read_mono16k(src)
    dur = mono.size / sr
    subs = [{"start": s, "end": min(s + SUB_LEN, dur)}
            for s in np.arange(0.0, max(dur - SUB_LEN - 0.5, 0.0), SUB_HOP)]
    if len(subs) < 3:
        raise SystemExit(f"素材太短（{dur:.1f}s），至少需要几条字幕")
    sources = [{"wav_path": str(src), "subs": subs}]
    S._load_embedder()
    print(f"素材 {src.name}  {dur:.0f}s / 字幕 {len(subs)} 条 / embedder={S.embedder_name()}")

    t0 = time.perf_counter()
    new_res = S.generate_speakers_project(sources)
    t_new = time.perf_counter() - t0

    orig = S._ECAPA_EMBED_IMPL
    S._ecapa_embedding = lambda m, s, a, b: orig(m, s, a, b)   # 迫使走逐条
    try:
        t0 = time.perf_counter()
        old_res = S.generate_speakers_project(sources)
        t_old = time.perf_counter() - t0
    finally:
        S._ecapa_embedding = orig

    print(f"批量 {t_new:.2f}s / 逐条 {t_old:.2f}s -> {t_old / max(t_new, 1e-9):.1f}x\n")

    ln, lo = new_res["items"][0], old_res["items"][0]
    checks: list[tuple[str, bool, str]] = [
        ("quality", new_res["quality"] == old_res["quality"],
         f"{new_res['quality']} vs {old_res['quality']}"),
        ("n_speakers", new_res["n_speakers"] == old_res["n_speakers"],
         f"{new_res['n_speakers']} vs {old_res['n_speakers']}"),
        ("total/labeled", (ln["total"], ln["labeled"]) == (lo["total"], lo["labeled"]),
         f"{ln['total']}/{ln['labeled']} vs {lo['total']}/{lo['labeled']}"),
        ("mixed 数", ln["mixed"] == lo["mixed"], f"{ln['mixed']} vs {lo['mixed']}"),
    ]

    def _segs(item: dict) -> list:
        return [(round(x["start"], 3), round(x["end"], 3), x.get("label"))
                for x in item.get("speaker_segments") or []]

    sn, so = _segs(ln), _segs(lo)
    checks.append((f"speaker_segments ({len(sn)})", sn == so, "逐段 start/end/label 比对"))

    lab_n = [x.get("label") for x in ln.get("sub_labels") or []]
    lab_o = [x.get("label") for x in lo.get("sub_labels") or []]
    checks.append((f"sub_labels ({len(lab_n)})", lab_n == lab_o, "逐条字幕标签比对"))

    kn, ko = set(new_res["label_embeddings"]), set(old_res["label_embeddings"])
    checks.append(("标签集合", kn == ko, f"{len(kn)} 个"))

    cos_min = None
    if kn == ko and kn:
        cos_min = min(
            float(np.dot(new_res["label_embeddings"][k], old_res["label_embeddings"][k]) /
                  (np.linalg.norm(new_res["label_embeddings"][k]) *
                   np.linalg.norm(old_res["label_embeddings"][k]) + 1e-12))
            for k in kn)

    width = max(len(name) for name, _, _ in checks)
    all_ok = True
    for name, ok, detail in checks:
        all_ok &= ok
        print(f"  {name:<{width}}  {'✓ 一致' if ok else '✗ 有差异'}   {detail}")
    if cos_min is not None:
        print(f"  {'质心余弦':<{width}}  min={cos_min:.8f}   (1.0 = 完全一致)")

    if not all_ok:
        print("\n❌ 批量路径改变了产物，需要复核批量实现")
        return 1
    print("\n✅ 批量路径与逐条路径决策完全一致：批量化对该素材零产物影响")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

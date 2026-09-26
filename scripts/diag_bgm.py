"""诊断：BGM 是否主导 ECAPA 嵌入 + CMS（素材内均值消减）能否恢复可分性。

只读诊断脚本：对 2 个素材计算字幕级嵌入（GPU），输出
1) 原始成对余弦相似度分布（BGM 抬升证据）
2) 三种空间下的聚类对比：raw / CMS(素材内均值消减) / CMS+白化
用法：.venv/Scripts/python.exe scripts/diag_bgm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from app import speakers as sp
from app.subtitles import parse_srt

ITEMS = ["m-c2f20f0598", "m-035b407134"]  # 最重素材 + 一个中等素材
WORKDIR = Path("workdir")


def load_subs(item_id):
    srt = WORKDIR / "subs" / f"{item_id}.srt"
    if not srt.exists():
        return []
    return [ {"start": ln.start, "end": ln.end, "text": ln.text}
             for ln in parse_srt(srt.read_text(encoding="utf-8")) ]


def item_embeddings(item_id, subs):
    mono, sr = sp.read_mono16k(str(WORKDIR / "items" / f"{item_id}.wav"))
    acc, sub_embs = sp._collect_source(sp._ecapa_embedding, mono, sr, subs, 0, len(subs))
    idxs = [i for i, e in enumerate(sub_embs) if e is not None]
    X = np.stack([sub_embs[i] for i in idxs])
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    return idxs, X


def pairwise_stats(X, name):
    n = len(X)
    rng = np.random.default_rng(0)
    sel = rng.choice(n, size=min(300, n), replace=False)
    S = X[sel] @ X[sel].T
    off = S[np.triu_indices(len(sel), 1)]
    print(f"  {name}: 成对cos p5={np.percentile(off,5):.3f} 中位={np.median(off):.3f} "
          f"p95={np.percentile(off,95):.3f}")


def cluster_report(X, name, min_k=3, max_k=15):
    n = len(X)
    if n < 10:
        return
    labs = sp._cluster_labels(list(X))
    from collections import Counter
    sizes = Counter(labs)
    sil = sp._silhouette_score(X, labs)
    top = sizes.most_common(6)
    print(f"  {name}: k={len(sizes)} silhouette={sil:.3f} | 分布 {[(c, s, f'{s/n:.0%}') for c, s in top]}")


def main():
    data = {}
    for iid in ITEMS:
        subs = load_subs(iid)
        if len(subs) < 20:
            print(f"{iid}: 字幕不足({len(subs)})，跳过")
            continue
        print(f"{iid}: {len(subs)} 行字幕，计算嵌入…")
        idxs, X = item_embeddings(iid, subs)
        data[iid] = (idxs, X)

        pairwise_stats(X, "raw")
        # CMS：减去素材内均值（消恒定 BGM 床）再归一化
        mu = X.mean(axis=0)
        Xc = X - mu
        Xc = Xc / (np.linalg.norm(Xc, axis=1, keepdims=True) + 1e-9)
        pairwise_stats(Xc, "CMS")

    # 跨素材合并聚类（模拟项目级联合聚类）
    all_raw = np.concatenate([X for _, X in data.values()])
    # 每素材各自的均值
    mus = np.concatenate([np.tile(X.mean(axis=0), (len(X), 1)) for _, X in data.values()])
    all_cms = all_raw - mus
    all_cms = all_cms / (np.linalg.norm(all_cms, axis=1, keepdims=True) + 1e-9)

    print(f"\n合并 {sum(len(X) for _, X in data.values())} 行做项目级聚类对比：")
    cluster_report(all_raw, "raw")
    cluster_report(all_cms, "CMS")


if __name__ == "__main__":
    main()

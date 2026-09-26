"""验证 CMS+Viterbi 新管线（零写入，不动 DB/角色池）。

对鹿目圆项目全部素材跑项目级联合识别，输出 n_speakers 与标签分布。
用法：.venv/Scripts/python.exe scripts/diag_full_project.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3

from app import speakers as sp
from app.subtitles import parse_srt

PROJECT = "p-2228d82dc9"


def main():
    db = sqlite3.connect("workdir/voicecut.db")
    item_ids = [r[0] for r in db.execute(
        "select id from items where project_id=?", (PROJECT,))]
    sources = []
    for iid in item_ids:
        srt = Path(f"workdir/subs/{iid}.srt")
        if not srt.exists():
            continue
        subs = [{"start": ln.start, "end": ln.end, "text": ln.text}
                for ln in parse_srt(srt.read_text(encoding="utf-8"))]
        if subs:
            sources.append({"wav_path": f"workdir/items/{iid}.wav", "subs": subs})
    total = sum(len(s["subs"]) for s in sources)
    print(f"素材 {len(sources)} 个 | 字幕 {total} 行，开始联合识别…")

    def prog(p):
        sys.stdout.write(f"\r进度 {p:.0%} ")
        sys.stdout.flush()

    res = sp.generate_speakers_project(sources, progress_cb=prog)
    print(f"\nn_speakers={res['n_speakers']} quality={res['quality']}")
    from collections import Counter
    sizes = Counter()
    for it in res["items"]:
        for sl in it["sub_labels"]:
            if sl["label"]:
                sizes[sl["label"]] += 1
    n_all = sum(sizes.values())
    for lb, n in sizes.most_common():
        print(f"  {lb}: {n} ({n / n_all:.1%})")
    print("mixed 行/素材:", [it["mixed"] for it in res["items"]])


def json_ids(data):
    import json
    try:
        d = json.loads(data) if data else {}
        ids = d.get("itemIds") or d.get("items") or []
        return [x["id"] if isinstance(x, dict) else x for x in ids]
    except Exception:
        return []


if __name__ == "__main__":
    main()

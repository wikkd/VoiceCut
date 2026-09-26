"""从 SQLite 快照里找回被覆盖/误删的片段（dry-run 默认，--apply 才写回）。

背景（2026-09-26）：自动化探针按坐标点行时命中了「删除」按钮，
客户端 deleteSegment → scheduleSaveProject → POST /api/items/<id>/project，
服务端 upsert_project 覆盖 item_projects.segments 列，那一段就"没了"。

为什么能捞回来：库是 WAL 模式，`workdir/voicecut.db` 主文件只在 checkpoint 时被改写，
所以**主文件单读**（不带同目录的 -wal）拿到的是上一次 checkpoint 的状态，
其中还留着删除前的 segments。而 -wal 里是 checkpoint 之后的所有改动（含用户自己的后续编辑），
所以**不能整行回滚** —— 只能把「快照里多出来的那几条」按原索引插回当前行。

用法：
  python scripts/recover_deleted_segment.py                # 只体检，列出所有"快照比当前多"的素材与缺失片段
  python scripts/recover_deleted_segment.py --apply        # 备份当前行并把缺失片段按原索引插回
  python scripts/recover_deleted_segment.py --item m-xxxx  # 只看指定素材
  python scripts/recover_deleted_segment.py --base URL     # 指定服务地址（默认 http://127.0.0.1:8765）
"""
import argparse
import json
import shutil
import sqlite3
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WD = ROOT / "workdir"
OUT = WD / "_recover"


def http_json(url, payload=None, timeout=120):
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def snapshot_rows(label, path: Path):
    """读一份库（不带 -wal）的 item_projects 快照 → {item_id: segments}。"""
    tmp = Path(tempfile.mkdtemp(prefix="vc-recover-"))
    dst = tmp / f"{label}.db"
    shutil.copyfile(path, dst)
    con = sqlite3.connect("file:" + dst.as_posix() + "?mode=ro", uri=True)
    out = {}
    try:
        for item_id, segs in con.execute("select item_id, segments from item_projects"):
            try:
                out[item_id] = json.loads(segs)
            except Exception:
                pass
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def collect_snapshots(only):
    """候选快照。默认只用**主库单读**（= 最近一次 checkpoint，通常就是几小时内的状态）。

    ⚠️ 不要默认拿 workdir/voicecut.db.bak-* 一起比：那些是隔代备份，用户后来重切过片段
    （id 全变），按 id 比会产出成千上万条"缺失"，纯噪声。需要时用 --snapshot 显式指定。
    """
    if only:
        p = Path(only)
        if not p.is_absolute():
            p = WD / p
        snaps = [(p.name, snapshot_rows(p.name, p))]
    elif (WD / "voicecut.db").exists():
        snaps = [("voicecut.db(最近一次checkpoint)", snapshot_rows("main", WD / "voicecut.db"))]
    else:
        snaps = []
    for name, rows in snaps:
        print(f"[snapshot] {name}：{len(rows)} 个素材")
    return snaps


BIG = 20   # 单个素材差异超过这个数：几乎一定是"快照隔代/片段被重切"，而不是误删


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="把缺失片段按原索引插回（必须同时给 --item，避免误写全库）")
    ap.add_argument("--item", help="只处理指定素材 id")
    ap.add_argument("--snapshot", help="显式指定快照库文件（默认用 workdir/voicecut.db 单读）")
    ap.add_argument("--all-snapshots", action="store_true", help="连同 workdir/voicecut.db.bak-* 一起扫（噪声大）")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()

    if args.apply and not args.item:
        print("✗ --apply 必须同时指定 --item，避免批量误写")
        return

    live = {}
    for r in http_json(f"{args.base}/api/projects"):
        for it in http_json(f"{args.base}/api/projects/{r['id']}")["items"]:
            st = http_json(f"{args.base}/api/items/{it['id']}/project")
            live[it["id"]] = list(st.get("segments") or [])
    print(f"[live] 素材 {len(live)} 个 · 片段合计 {sum(len(v) for v in live.values())}")

    snaps = []
    snaps += collect_snapshots(args.snapshot)
    if args.all_snapshots:
        for p in sorted(WD.glob("voicecut.db.bak-*")):
            snaps.append((p.name, snapshot_rows(p.name, p)))

    plan = {}
    for label, rows in snaps:
        for item_id, old in rows.items():
            if args.item and item_id != args.item:
                continue
            cur = live.get(item_id)
            if cur is None:
                continue
            cur_ids = {s.get("id") for s in cur}
            missing = [(k, s) for k, s in enumerate(old) if s.get("id") not in cur_ids]
            if not missing:
                continue
            best = plan.get(item_id)
            if best is None or len(missing) > len(best[2]):
                plan[item_id] = (label, cur, missing)

    if not plan:
        print("✓ 未发现任何「快照里有、当前没有」的片段")
        return
    total = 0
    for item_id, (label, cur, missing) in sorted(plan.items(), key=lambda kv: -len(kv[1][2])):
        total += len(missing)
        flag = "  ⚠️ 差异过大，多半是快照隔代/片段被重切，不是误删 —— 不要 --apply" if len(missing) > BIG else ""
        print(f"\n[!] {item_id}：快照 {label} 比当前多 {len(missing)} 段（当前 {len(cur)}）{flag}")
        for k, s in missing[:8]:
            print(f"    原索引 {k}: " + json.dumps(s, ensure_ascii=False)[:150])
        if len(missing) > 8:
            print(f"    …另 {len(missing) - 8} 段")
    print(f"\n合计差异 {total} 段" + (f"（涉及 {len(plan)} 个素材）" if len(plan) > 1 else ""))

    if not args.apply:
        print("（dry-run；确认清单无误后加 --apply --item <id> 才写回）")
        return

    item_id = args.item
    label, cur, missing = plan[item_id]
    st = http_json(f"{args.base}/api/items/{item_id}/project")
    OUT.mkdir(parents=True, exist_ok=True)
    bak = OUT / f"before_{item_id}_{int(time.time())}.json"
    bak.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    fixed = list(cur)
    for k, s in sorted(missing, key=lambda x: x[0]):
        fixed.insert(min(k, len(fixed)), s)
    http_json(f"{args.base}/api/items/{item_id}/project",
              {"segments": fixed, "speaker_segments": st.get("speaker_segments") or []})
    after = http_json(f"{args.base}/api/items/{item_id}/project")["segments"]
    ok = [s.get("id") for s in after[:len(fixed)]] == [s.get("id") for s in fixed]
    print(f"[apply] {item_id}: {len(cur)} → {len(after)} 段 · 顺序一致={ok} · 备份 {bak.name}")



main()

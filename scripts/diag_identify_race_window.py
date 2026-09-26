"""量化「识别 worker 从读入片段快照到整段写回」的竞态窗口（只读，不写真实数据）。

背景
----
`_project_speakers_run`（项目级识别）与 `_speakers_worker`（单素材识别）的写回都是
「load_project → normalize_segments → bind_segments → save_project」**整段数组覆盖**。
三步里对人工成果的保护都靠 `seg.get("locked")`：

- `normalize_segments`：`not p.get("locked") and not s.get("locked")` 才合并
- `bind_segments`   ：`if seg.get("locked"): out.append(seg); continue`
- `bind_segments`   ：判为 mixed 时 `seg["characterId"] = None`（清空人工指派）

但这些判定读的是 **load_project 那一刻的快照**。前端改说话人时会把片段标
`locked=true` 并通过 400ms 去抖 POST 落盘；若这次 POST 恰好落在
`load_project … save_project` 之间，写回就会用聚类结果覆盖人工指派，用户看到的
现象是「改了说话人，识别结束后又被改回去 / 变成未分配」。

本脚本做两件事（全程只读真实数据，save 计时写临时目录）：
1. 在真实 workdir 上量出窗口内各步耗时，并给出「用户保存撞进窗口」的量级；
2. 用真实片段做**不变量体检**：locked 片段经 normalize + bind 后是否原样保留
   （含被判 mixed 时 characterId 是否被清空），并给一个未 locked 的对照。

用法：
    .venv/Scripts/python.exe scripts/diag_identify_race_window.py [workdir]
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_mod  # noqa: E402
from app import project as pr  # noqa: E402
from app import speakers as sp  # noqa: E402

DEBOUNCE_MS = 400          # store.js scheduleSaveProject 的去抖
EDIT_INTERVAL_MS = 3000    # 用户连续修正说话人的典型间隔（保守估计）


def _timeit(fn, *a, **kw):
    t0 = time.perf_counter()
    r = fn(*a, **kw)
    return r, (time.perf_counter() - t0) * 1000.0


def main() -> int:
    workdir = Path(sys.argv[1] if len(sys.argv) > 1 else "workdir")
    ids = sorted({p.stem for p in (workdir / "items").glob("*.wav")}
                 | {p.stem for p in (workdir / "items").glob("*.mp4")})
    if not ids:
        print(f"未找到任何素材（workdir={workdir}）")
        return 1
    print(f"workdir = {workdir.resolve()}   候选素材 {len(ids)} 个，加载中…")

    # ── 全项目 locked 规模（人工成果有多少）共用一个连接 ──
    loaded: list[tuple[str, list]] = []
    for i in ids:
        try:
            pj = pr.load_project(workdir, i)
        except Exception:
            continue
        segs = pj.get("segments") or []
        if segs:
            loaded.append((i, segs))
    if not loaded:
        print("没有任何素材带片段数据")
        return 1
    total_segs = sum(len(s) for _, s in loaded)
    total_locked = sum(1 for _, ss in loaded for s in ss if s.get("locked"))
    print(f"有片段的素材 {len(loaded)} 个；片段合计 {total_segs}，"
          f"其中 locked（人工成果）{total_locked} 个\n")

    # ── 取最大素材量窗口 ──
    biggest_id, biggest_segs = max(loaded, key=lambda kv: len(kv[1]))
    print(f"[窗口分量] 最大素材 {biggest_id}：{len(biggest_segs)} 段"
          f"（locked {sum(1 for s in biggest_segs if s.get('locked'))}）")
    _, t_load = _timeit(pr.load_project, workdir, biggest_id)
    print(f"  load_project                {t_load:8.2f} ms")

    norm, t_norm = _timeit(sp.normalize_segments, biggest_segs)
    print(f"  normalize_segments          {t_norm:8.2f} ms  (移除 {norm[1]} 段)")

    dur = max((float(s.get("end") or 0) for s in norm[0]), default=1.0) or 1.0
    spk_segs = [{"start": 0.0, "end": dur, "label": "SPK_A"}]
    bound, t_bind = _timeit(sp.bind_segments, norm[0], spk_segs, {"SPK_A": "cAUTO"},
                            new_id=pr.new_uid)
    print(f"  bind_segments               {t_bind:8.2f} ms  (mixed {bound[1]})")

    _, t_dump = _timeit(json.dumps, bound[0], ensure_ascii=False)
    print(f"  json.dumps(segments)        {t_dump:8.2f} ms")
    with tempfile.TemporaryDirectory(prefix="vc-race-") as td:
        _, t_save = _timeit(pr.save_project, Path(td), biggest_id,
                            {"segments": bound[0], "speaker_segments": spk_segs})
        db_mod.reset_conns()   # 关掉临时库连接，否则临时目录删不掉（WinError 32）
    print(f"  save_project（写临时目录）    {t_save:8.2f} ms")

    window = t_norm + t_bind + t_dump + t_save
    print(f"\n窗口合计 ≈ {window:.0f} ms（load 之后到落盘之间；单素材）")
    est = window / EDIT_INTERVAL_MS
    print(f"若用户每 {EDIT_INTERVAL_MS} ms 修正一次说话人，单素材一轮窗口内被覆盖的期望"
          f"次数 ≈ {est:.2f}；项目共 {len(loaded)} 个素材 → ≈ {est * len(loaded):.2f} 次/轮")
    print(f"（前端保存去抖 {DEBOUNCE_MS} ms：用户的保存落在窗口内即被写回覆盖）")

    # ── 不变量体检：locked 片段必须原样保留 ──
    print("\n[不变量体检] locked 片段经 normalize + bind 后是否原样保留")
    probe = {
        "id": "s_probe", "start": 1.0, "end": 3.0, "text": "人工改过",
        "language": "JP", "speakerLabel": "人工标", "characterId": "cUSER",
        "locked": True,
    }
    dup = {**probe, "id": "s_dup", "text": "重复碎片",
           "characterId": None, "locked": False}
    # 注意：probe（locked）与 dup 同起点同终点 → 满足去重的全部几何条件。
    # 保护生效的判据是「**一个都不删**」——锁定段既不自己被删，也不吞噬重复碎片。
    n_out, n_removed = sp.normalize_segments([dict(probe), dup])
    n_ids = sorted(s["id"] for s in n_out)
    ok_norm = n_removed == 0 and n_ids == ["s_dup", "s_probe"]
    print(f"  normalize 锁定段不被删也不吞噬重复碎片: {'OK' if ok_norm else 'FAIL'} "
          f"(removed={n_removed}, ids={n_ids})")
    # 对照：同一对片段去掉 locked → 照常去重（证明上面 OK 不是判据失灵）
    _, n_removed2 = sp.normalize_segments([{**probe, "locked": False}, dup])
    ok_norm2 = n_removed2 == 1
    print(f"  对照（快照里没有 locked）: removed={n_removed2}"
          f" → {'照常去重 OK' if ok_norm2 else '判据异常'}")

    two = [{"start": 1.0, "end": 2.0, "label": "SPK_A"},
           {"start": 2.0, "end": 3.0, "label": "SPK_B"}]
    cid = {"SPK_A": "cA", "SPK_B": "cB"}
    b_out, _ = sp.bind_segments([dict(probe)], two, cid, new_id=pr.new_uid)
    kept = b_out[0]
    ok_bind = kept.get("locked") is True and kept.get("characterId") == "cUSER"
    print(f"  bind 保留 locked 段的 characterId:     {'OK' if ok_bind else 'FAIL'} "
          f"(locked={kept.get('locked')}, characterId={kept.get('characterId')!r})")

    s_out, _ = sp.bind_segments([{**probe, "locked": False}], two, cid,
                                new_id=pr.new_uid)
    stale_cid = s_out[0].get("characterId")
    print(f"  对照（快照里没有 locked）: characterId={stale_cid!r} "
          f"locked={s_out[0].get('locked')!r}"
          f" → {'人工指派被覆盖（这就是『有概率』的另一半）' if stale_cid != 'cUSER' else '未被覆盖'}")

    ok = ok_norm and ok_norm2 and ok_bind
    print(f"\n结论：保护规则本身{'完整' if ok else '不完整'}；"
          f"风险来自「快照 vs 落盘」的时间差 —— 窗口 ≈ {window:.0f} ms/素材")
    print("      修复：识别/反馈写回统一走 project.save_project_guarding_locked，"
          "在 db 写锁内重读磁盘收敛 locked（窗口→0）；")
    print("      测试：tests/test_identify_no_clobber.py + scripts/browser_test.js 的 NCLB 段。")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())

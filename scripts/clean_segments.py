"""存量片段数据清洗：去除识别重跑产生的重复/碎片片段（棘轮清理）。

用法:
    .venv/Scripts/python.exe scripts/clean_segments.py            # 试运行，只报告
    .venv/Scripts/python.exe scripts/clean_segments.py --apply    # 写回

⚠️ --apply 前必须先停掉 voicecut 服务与所有已打开的页面（前端内存中的旧
片段列表会在防抖保存时把脏数据写回）。脚本会先自动备份 voicecut.db。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.speakers import normalize_segments  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv
    db_path = ROOT / "workdir" / "voicecut.db"
    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return
    if apply:
        bak = db_path.with_name(f"voicecut.db.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(db_path, bak)
        print(f"已备份: {bak}")
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT item_id, segments FROM item_projects WHERE segments IS NOT NULL").fetchall()
    names = dict(conn.execute("SELECT id, name FROM items"))
    total_before = total_after = total_removed = 0
    touched = 0
    for item_id, raw in rows:
        try:
            segs = json.loads(raw)
        except Exception:
            continue
        if not isinstance(segs, list) or not segs:
            continue
        cleaned, removed = normalize_segments(segs)
        total_before += len(segs)
        total_after += len(cleaned)
        total_removed += removed
        if removed:
            touched += 1
            print(f"  {item_id} {names.get(item_id, '')[:28]:30s} "
                  f"{len(segs)} -> {len(cleaned)}  (-{removed})")
            if apply:
                conn.execute("UPDATE item_projects SET segments=? WHERE item_id=?",
                             (json.dumps(cleaned, ensure_ascii=False), item_id))
    if apply:
        conn.commit()
    conn.close()
    mode = "已写回" if apply else "试运行（加 --apply 写回）"
    print(f"\n[{mode}] 片段总数 {total_before} -> {total_after} "
          f"(清理 {total_removed}，涉及 {touched} 个素材)")


if __name__ == "__main__":
    main()

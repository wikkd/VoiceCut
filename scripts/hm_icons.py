"""从 HarmonyOS_Icons.zip 里提取选定图标，并生成一张可视化联络表（contact sheet）。

用法：
  python scripts/hm_icons.py list                 # 列出全部图标名
  python scripts/hm_icons.py sheet <out.html>     # 生成联络表 HTML（内置 base64，离线可看）
  python scripts/hm_icons.py pick <out_dir>       # 按 PICKS 清单提取到 out_dir
"""
from __future__ import annotations

import base64
import sys
import zipfile
from pathlib import Path

ZIP = Path(r"D:\harmony资源包\HarmonyOS_Icons.zip")

# 候选：覆盖 VoiceCut 的菜单栏 / 传输栏 / 面板 / 片段列表 / 角色池 / 训练页所需语义
CANDIDATES = [
    # 通用动作
    "ic_public_close", "ic_public_close_filled", "ic_public_add", "ic_public_add_norm",
    "ic_public_more", "ic_public_more_list", "ic_public_delete", "ic_public_delete_filled",
    "ic_public_remove", "ic_public_list_remove", "ic_public_edit", "ic_public_save",
    "ic_public_ok", "ic_public_ok_filled", "ic_public_cancel", "ic_public_error",
    "ic_public_fail", "ic_public_help", "ic_public_quit", "ic_public_reset",
    "ic_public_refresh", "ic_public_share", "ic_public_copy", "ic_public_scan",
    # 导航 / 箭头
    "ic_public_back", "ic_public_backtotop", "ic_public_arrow_left", "ic_public_arrow_right",
    "ic_public_arrow_up_0", "ic_public_topping", "ic_public_navigation",
    "ic_public_drawer", "ic_public_home", "ic_public_detail", "ic_public_search",
    # 文件 / 数据
    "ic_public_folder", "ic_public_folder_filled", "ic_public_file", "ic_public_file_filled",
    "ic_public_download", "ic_public_upload", "ic_public_cloud_download", "ic_public_cloud_upload",
    "ic_public_albums", "ic_public_picture", "ic_public_cards", "ic_public_storage",
    "ic_public_text", "ic_public_code", "ic_public_calendar", "ic_public_history",
    # 媒体 / 播放
    "ic_public_play", "ic_public_play_norm", "ic_public_pause", "ic_public_pause_norm",
    "ic_public_play_next", "ic_public_play_last", "ic_public_order_play",
    "ic_public_single_cycle", "ic_public_list_cycle", "ic_public_shuffle",
    "ic_public_sound", "ic_public_sound_off", "ic_public_volume_down", "ic_public_voice",
    "ic_public_voice_filled",
    # 音频处理 / 领域动作
    "ic_public_clean", "ic_public_clean_filled", "ic_merge", "ic_public_highlight",
    "ic_public_enlarge", "ic_public_reduce", "ic_public_drag_handle", "ic_public_drag_handle_filled",
    "ic_public_list_add_light", "ic_public_list_add_transparent", "ic_public_select all",
    "ic_public_deselect_all", "ic_public_emoji", "ic_public_themes", "ic_public_community_messages",
    # 人 / 角色
    "ic_public_contacts", "ic_public_contacts_filled", "ic_public_contacts_group",
    "ic_public_contacts_group_filled", "ic_public_face", "ic_public_face_filled",
    # 时间
    "ic_public_time", "ic_public_timer", "ic_public_stopwatch", "ic_public_clock",
    "ic_public_worldclock",
    # 视图 / 设置
    "ic_public_settings", "ic_public_view_list", "ic_public_view_grid", "ic_public_search_filled",
    "ic_public_fast", "ic_public_fast_filled", "ic_public_spinner", "ic_public_spinner_small",
    "ic_public_security", "ic_public_privacy", "ic_public_connection", "ic_public_upgrade",
    # 设备/AI（自动训练用）
    "ic_device_sound_ai", "ic_device_sound_ai_filled", "ic_device_audio", "ic_public_app",
    # 实际落地时才补进来的一批（映射定稿后回填，避免以后重新翻包）
    "ic_gallery_fullscreen", "ic_public_multiscreen", "ic_public_rotate", "ic_public_sift",
    "ic_public_storage", "ic_public_todo", "ic_public_topping", "ic_public_upgrade",
    "ic_public_card", "ic_public_cards", "ic_public_albums", "ic_public_collect",
    "ic_public_switch_audio", "ic_public_quickstart", "ic_public_highlight",
]


# 最终入库清单（`pick` 的默认值）：与 app/static/vendor/harmony-icons/ 逐字节一致。
# 只放**被 style.css 引用**的图标。映射调整过程中删掉过 9 个：
#   albums（→storage）/ highlight（→gallery_fullscreen）/ history（→reset）
#   contacts_group_filled（原计划做"角色池激活态"，但池是全屏覆盖层，按钮看不见 → 放弃）
#   app / save / settings / upload / select_all（本来就没有对应功能，不挂空类）
# 一致性由 scripts/check_icons.py 双向校验（CSS 引用 ⊆ 目录、目录 ⊆ CSS 引用），
# 且已接进 CI 的 Icon check 步；要换图标改完 CSS 后跑一次 pick 即可。
FINAL = [
    "ic_gallery_fullscreen", "ic_merge", "ic_public_add", "ic_public_arrow_right",
    "ic_public_arrow_up_0", "ic_public_back", "ic_public_clean", "ic_public_close",
    "ic_public_contacts_group", "ic_public_delete", "ic_public_download", "ic_public_drag_handle",
    "ic_public_edit", "ic_public_enlarge", "ic_public_fail", "ic_public_fast",
    "ic_public_folder", "ic_public_help", "ic_public_lock", "ic_public_more_list",
    "ic_public_multiscreen", "ic_public_ok", "ic_public_pause", "ic_public_picture",
    "ic_public_play", "ic_public_play_last", "ic_public_play_next", "ic_public_reduce",
    "ic_public_refresh", "ic_public_reset", "ic_public_rotate", "ic_public_scan",
    "ic_public_search", "ic_public_sift", "ic_public_single_cycle", "ic_public_sound",
    "ic_public_storage", "ic_public_text", "ic_public_todo", "ic_public_unlock",
    "ic_public_view_grid", "ic_public_view_list", "ic_public_voice",
]


def names() -> list[str]:
    with zipfile.ZipFile(ZIP) as z:
        return sorted(z.namelist())


def pick(names_wanted: list[str], out_dir: Path) -> tuple[list[str], list[str]]:
    """提取；返回 (已提取, 缺失)。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP) as z:
        have = {n.rsplit("/", 1)[-1]: n for n in z.namelist()}
        done, missing = [], []
        for w in names_wanted:
            key = w if w.lower().endswith(".svg") else w + ".svg"
            src = have.get(key)
            if not src:
                missing.append(key)
                continue
            (out_dir / key).write_bytes(z.read(src))
            done.append(key)
    return done, missing


def sheet(out_html: Path, wanted: list[str], big: int = 32) -> tuple[int, list[str]]:
    small = max(12, big * 5 // 8)
    with zipfile.ZipFile(ZIP) as z:
        have = {n.rsplit("/", 1)[-1]: n for n in z.namelist()}
        rows, shown, missing = [], [], []
        for w in wanted:
            key = w if w.lower().endswith(".svg") else w + ".svg"
            src = have.get(key)
            if not src:
                missing.append(key)
                continue
            b64 = base64.b64encode(z.read(src)).decode()
            shown.append(key)
            # 每个图标放三种底色/尺寸，检查在深底/浅底/小尺寸下的可读性
            rows.append(
                f'<figure><div class="strip">'
                f'<img src="data:image/svg+xml;base64,{b64}" style="width:{big}px">'
                f'<img src="data:image/svg+xml;base64,{b64}" style="width:{small}px">'
                f'<img class="inv" src="data:image/svg+xml;base64,{b64}" style="width:{big}px">'
                f'<img class="inv" src="data:image/svg+xml;base64,{b64}" style="width:{small}px">'
                f'</div><figcaption>{key[:-4]}</figcaption></figure>')
    name_w = max(190, big * 6)
    html = f"""<!doctype html><meta charset="utf-8"><title>HarmonyOS 图标联络表</title>
<style>
 body {{ background:#f5f6f8; color:#1a1a1a; font:12px/1.4 system-ui,"Microsoft YaHei",sans-serif; margin:16px;
        display:grid; grid-template-columns:repeat(auto-fill,minmax({name_w}px,1fr)); gap:10px; }}
 figure {{ margin:0; background:#fff; border:1px solid #d8dde3; border-radius:8px; padding:8px; }}
 .strip {{ display:flex; align-items:center; gap:12px; height:{big + 8}px; }}
 .inv {{ filter:invert(1); background:#0f1216; border-radius:4px; padding:3px; }}
 figcaption {{ margin-top:6px; font-size:11px; color:#5a6a7a; word-break:break-all; }}
</style>
<h1 style="grid-column:1/-1;font-size:14px;margin:0 0 4px">共 {len(shown)} 个候选（左→右：{big}px 浅底 / {small}px 浅底 / {big}px 深底 / {small}px 深底）</h1>
{''.join(rows)}"""
    out_html.write_text(html, encoding="utf-8")
    return len(shown), missing


# 放大核对：最关键的图标（要替换 emoji 的那一批 + 语义需要确认的）
CRITICAL = [
    "ic_public_lock", "ic_public_lock_filled", "ic_public_unlock", "ic_public_unlock_filled",
    "ic_public_play", "ic_public_pause", "ic_public_play_next", "ic_public_play_last",
    "ic_public_backtotop", "ic_public_single_cycle",
    "ic_public_clean", "ic_merge", "ic_public_scan", "ic_public_voice",
    "ic_public_contacts_group", "ic_public_contacts_group_filled",
    "ic_public_drag_handle", "ic_public_more_list", "ic_public_list_add_light",
    "ic_public_history", "ic_public_reset", "ic_public_refresh",
    "ic_public_save", "ic_public_text", "ic_device_sound_ai", "ic_public_highlight",
    "ic_public_albums", "ic_public_picture", "ic_public_enlarge", "ic_public_reduce",
    "ic_public_ok", "ic_public_fail", "ic_public_error", "ic_public_select all",
    "ic_public_view_grid", "ic_public_folder", "ic_public_settings",
]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for n in names():
            print(n)
    elif cmd == "sheet":
        out = Path(sys.argv[2])
        wanted = CRITICAL if "--critical" in sys.argv else CANDIDATES
        big = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--size=")), 32))
        n, miss = sheet(out, wanted, big)
        print(f"联络表: {out}  已渲染 {n} 个 (size={big}px)")
        if miss:
            print("缺失:", miss)
    elif cmd == "pick":
        out = Path(sys.argv[2])
        done, miss = pick([Path(p).stem for p in sys.argv[3:]] or FINAL, out)
        print(f"提取 {len(done)} 个 → {out}")
        if miss:
            print("缺失:", miss)
    else:
        print(__doc__)

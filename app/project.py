"""Per-item project state persistence (characters, segments, speaker segments).

Stored in the SQLite DB (workdir/voicecut.db, table ``projects``) so the
character pool / segments / speaker labels survive page refresh and restarts.
The legacy JSON files under workdir/projects/ are migrated once by app.db.

Client-facing data shapes (plain dicts):
  Character        = {id, name, color, speakerLabels: [...], created}
  Segment          = {id, start, end, text, language, speakerLabel, characterId, note?}
  speaker_segment  = {start, end, label}
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from app import db

PROJECT_VERSION = 1

# Auto character palette (distinct hues on dark UI)
PALETTE = [
    "#e5484d", "#f76808", "#f5d90a", "#46a758", "#3e63dd",
    "#8e4ec6", "#12a594", "#e93d82", "#00a2c7", "#ffb224",
]

_color_state = {"n": 0}


def new_uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def next_color() -> str:
    c = PALETTE[_color_state["n"] % len(PALETTE)]
    _color_state["n"] += 1
    return c


def default_project() -> dict:
    return {
        "version": PROJECT_VERSION,
        "characters": [],
        "segments": [],
        "speaker_segments": [],
    }


def project_path(workdir: Path, item_id: str) -> Path:
    """Legacy JSON path (unused after migration; kept for back-compat/tests)."""
    return Path(workdir) / "projects" / f"{item_id}.json"


def _json_list(raw: str | None, fallback: list) -> list:
    """解析 JSON 列表列；空/损坏时回退到 fallback。"""
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return v
        except Exception:
            pass
    return fallback


def load_project(workdir: Path, item_id: str) -> dict:
    conn = db.get_conn(workdir)
    cols = db.fetch_project_columns(conn, item_id)
    if cols is None:
        return default_project()
    data_raw, seg_raw, spk_raw = cols
    try:
        data = json.loads(data_raw or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("characters", [])
    data["segments"] = _json_list(seg_raw, data.get("segments") or [])
    data["speaker_segments"] = _json_list(spk_raw, data.get("speaker_segments") or [])
    return data


def _write_project(conn, item_id: str, data: dict) -> None:
    """真正落库：segments / speaker_segments 走独立列（紧凑 JSON），其余进 data 列。"""
    segs = data.get("segments") or []
    spks = data.get("speaker_segments") or []
    rest = {k: v for k, v in data.items() if k not in ("segments", "speaker_segments")}
    db.upsert_project(
        conn, item_id,
        json.dumps(rest, ensure_ascii=False),
        segments=json.dumps(segs, ensure_ascii=False),
        speaker_segments=json.dumps(spks, ensure_ascii=False),
    )


def save_project(workdir: Path, item_id: str, data: dict) -> dict:
    """持久化 per-item 项目状态（**整段覆盖**，不做任何保护性合并）。

    用于前端保存、重置清理等「调用方明确知道自己在写什么」的场景。
    识别/反馈类后台任务必须在 load 快照与落盘之间保持人工成果时，改用
    :func:`save_project_guarding_locked`（见其文档）。
    """
    conn = db.get_conn(workdir)
    _write_project(conn, item_id, data)
    return data


def _span(s: dict):
    """片段时长区间；非法/零长返回 None。"""
    try:
        a = float(s.get("start", 0.0) or 0.0)
        b = float(s.get("end", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return (a, b) if b > a else None


def _overlap_ratio(a: dict, b: dict) -> float:
    """重叠占较短者长度的比例（不可比时 0.0）。"""
    sa, sb = _span(a), _span(b)
    if not sa or not sb:
        return 0.0
    ov = min(sa[1], sb[1]) - max(sa[0], sb[0])
    short = min(sa[1] - sa[0], sb[1] - sb[0])
    return ov / short if ov > 0 and short > 0 else 0.0


def merge_locked_segments(prev: list, incoming: list, *,
                          overlap_ratio: float = 0.5) -> list:
    """把磁盘上已锁定的人工片段收敛进待写列表（人工成果优先）。

    背景：说话人识别 / 声纹反馈 worker 是「load_project →（秒~分钟）→
    normalize_segments + bind_segments → save_project」的**整段覆盖写回**，
    而 ``normalize_segments`` / ``bind_segments`` 判定 ``locked`` 读的是 **load
    那一刻的快照**。用户改完说话人只经前端 400ms 防抖 POST 落盘，若这次 POST
    落在 worker 的 load…save 之间（实测窗口 ≈ 39 ms/素材，全项目 ≈ 0.24 次/轮），
    人工指派就被聚类结果覆盖。用户现象：「改完说话人又被识别改回去」（有概率）。

    这里以磁盘为准收敛：``prev`` 为落盘版本，凡其中 ``locked`` 的片段，
    - id 在 ``incoming`` 中存在 → 整条以 ``prev`` 为准（locked / characterId /
      text / language / speakerLabel / mixed 全部还原，人工改过的一律不被冲掉）；
    - id 不存在（被去重吞掉 / 被 mixed 判为拆分 / 被字幕重建替换）→ 先移除与之
      明显重叠（≥ ``overlap_ratio``）的非锁定片段，再把人工版本补回，不丢人工成果。

    无 locked 片段时原样返回 ``incoming``。注意：**有意**清空 locked 片段
    assignment 的调用方（``_reset_project_pool`` 重新识别前置清理、前端删除角色 /
    合并角色 / 用户显式改说话人）必须继续用 :func:`save_project`，不能走本通道。
    """
    locked_prev = [dict(s) for s in (prev or []) if s.get("locked") and s.get("id")]
    if not locked_prev:
        return list(incoming or [])
    by_id = {s["id"]: s for s in locked_prev}
    out: list = []
    for s in (incoming or []):
        p = by_id.get(s.get("id"))
        out.append(dict(p) if p is not None else s)
    have = {s.get("id") for s in out}
    for p in locked_prev:
        if p["id"] in have:
            continue
        out = [s for s in out
               if s.get("locked") or _overlap_ratio(p, s) < overlap_ratio]
        out.append(dict(p))
    return out


def save_project_guarding_locked(workdir: Path, item_id: str, data: dict) -> dict:
    """写回人工成果安全版：在 db 写锁内「重读磁盘 → 合并 locked → 写入」。

    识别 / 反馈 worker 写回片段时必须用它。关键是把 read-modify-write 收进同一个
    :func:`db.lock` 区间：本进程内所有写者（含前端保存 POST）都要先拿这把锁，
    于是「重读 + 合并 + 落库」对外表现为一次原子写，把原本 ≈39 ms 的
    load→save 窗口压到 0，杜绝聚类结果覆盖人工说话人指派。

    返回真正落库的数据（``segments`` 可能已被人工版本替换）。
    """
    conn = db.get_conn(workdir)
    with db.lock():
        cols = db.fetch_project_columns(conn, item_id)
        prev_segs = _json_list(cols[1], []) if cols else []
        out = dict(data)
        out["segments"] = merge_locked_segments(prev_segs, data.get("segments") or [])
        _write_project(conn, item_id, out)
    return out


def delete_project(workdir: Path, item_id: str) -> None:
    conn = db.get_conn(workdir)
    db.delete_project_row(conn, item_id)

# ---- project-level shared character pool -------------------------

def default_pool() -> dict:
    """Empty project character pool."""
    return {"characters": []}


def load_pool(workdir: Path, project_id: str) -> dict:
    """Load the project-level shared character pool (empty dict on missing)."""
    conn = db.get_conn(workdir)
    rec = db.fetch_project_record(conn, project_id)
    if rec is None:
        return default_pool()
    try:
        extra = json.loads(rec["extra"] or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    chars = extra.get("characters")
    if not isinstance(chars, list):
        chars = []
    return {"characters": chars}


def save_pool(workdir: Path, project_id: str, characters: list) -> dict:
    """Persist the project-level character pool; keeps other project settings.

    Returns {"characters": [...]}.
    """
    conn = db.get_conn(workdir)
    chars = list(characters or [])
    rec = db.fetch_project_record(conn, project_id)
    try:
        extra = json.loads(rec["extra"] or "{}") if rec else {}
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["characters"] = chars
    db.update_project_extra(conn, project_id, extra)
    return {"characters": chars}


def _load_extra(workdir: Path, project_id: str) -> dict:
    """Read the project record's raw extra dict (characters + settings)."""
    conn = db.get_conn(workdir)
    rec = db.fetch_project_record(conn, project_id)
    if rec is None:
        return {}
    try:
        extra = json.loads(rec["extra"] or "{}")
    except Exception:
        extra = {}
    return extra if isinstance(extra, dict) else {}


def get_project_setting(workdir: Path, project_id: str, key: str, default=None):
    """Read one project-level setting (e.g. auto_analyze) from the extra blob."""
    return _load_extra(workdir, project_id).get(key, default)


def set_project_setting(workdir: Path, project_id: str, key: str, value) -> None:
    """Persist one project-level setting, preserving the character pool."""
    conn = db.get_conn(workdir)
    rec = db.fetch_project_record(conn, project_id)
    if rec is None:
        return
    try:
        extra = json.loads(rec["extra"] or "{}")
    except Exception:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra[key] = value
    db.update_project_extra(conn, project_id, extra)

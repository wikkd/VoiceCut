"""项目 / 角色池 / 片段 / 说话人 路由（Blueprint）。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app import autosplit as autosplit_mod
from app import db as db_mod
from app import project as project_mod
from app import speakers as speakers_mod
from app import subtitles as subtitles_mod
from app import transcribe as transcribe_mod
from app.ffmpeg_util import detect_silence, media_duration
from app.tasks import TaskCancelled

bp = Blueprint("projects", __name__)


def ctx():
    return current_app.extensions["vc_ctx"]


# ── 项目 CRUD ─────────────────────────────────────────────

@bp.get("/api/projects")
def api_projects() -> object:
    c = ctx()
    conn = db_mod.get_conn(c.cfg.workdir)
    out = []
    for r in db_mod.fetch_project_records(conn):
        try:
            extra = json.loads(r["extra"] or "{}")
        except Exception:
            extra = {}
        out.append({
            "id": r["id"], "name": r["name"], "created": r["created"],
            "updated": r["updated"],
            "item_count": db_mod.count_items_in_project(conn, r["id"]),
            "character_count": len(extra.get("characters") or []),
        })
    return jsonify(out)


@bp.post("/api/projects")
def api_projects_create() -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        name = "新项目"
    pid = f"p-{uuid.uuid4().hex[:10]}"
    now = time.time()
    conn = db_mod.get_conn(c.cfg.workdir)
    db_mod.insert_project(conn, pid, name, now, now, {})
    return jsonify({"id": pid, "name": name, "created": now, "updated": now,
                    "item_count": 0, "character_count": 0})


@bp.get("/api/projects/<project_id>")
def api_project_record_get(project_id: str) -> object:
    c = ctx()
    conn = db_mod.get_conn(c.cfg.workdir)
    rec = db_mod.fetch_project_record(conn, project_id)
    if rec is None:
        return jsonify({"error": "project not found"}), 404
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    return jsonify({
        "id": rec["id"], "name": rec["name"], "created": rec["created"],
        "updated": rec["updated"],
        "auto_analyze": bool(project_mod.get_project_setting(
            c.cfg.workdir, project_id, "auto_analyze", True)),
        "characters": pool["characters"],
        "items": [c.item_json(i) for i in c.store.by_project(project_id)],
    })


@bp.post("/api/projects/<project_id>/rename")
def api_project_rename(project_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "名称为空"}), 400
    conn = db_mod.get_conn(c.cfg.workdir)
    if db_mod.fetch_project_record(conn, project_id) is None:
        return jsonify({"error": "project not found"}), 404
    db_mod.rename_project_record(conn, project_id, name)
    return jsonify({"ok": True})


@bp.delete("/api/projects/<project_id>")
def api_project_delete(project_id: str) -> object:
    c = ctx()
    conn = db_mod.get_conn(c.cfg.workdir)
    if db_mod.fetch_project_record(conn, project_id) is None:
        return jsonify({"error": "project not found"}), 404
    if db_mod.count_items_in_project(conn, project_id):
        return jsonify({"error": "项目非空，请先移除素材"}), 400
    db_mod.delete_project_record(conn, project_id)
    return jsonify({"ok": True})


# ── 角色池 ────────────────────────────────────────────────

@bp.get("/api/projects/<project_id>/characters")
def api_pool_get(project_id: str) -> object:
    c = ctx()
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    return jsonify({"characters": pool["characters"]})


@bp.post("/api/projects/<project_id>/characters")
def api_pool_save(project_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    chars = body.get("characters")
    if not isinstance(chars, list):
        return jsonify({"error": "characters must be a list"}), 400
    project_mod.save_pool(c.cfg.workdir, project_id, chars)
    return jsonify({"ok": True})


@bp.post("/api/projects/<project_id>/settings")
def api_project_settings(project_id: str) -> object:
    """项目设置（当前仅 auto_analyze：导入后后台自动生成字幕 + 识别说话人）。"""
    c = ctx()
    if db_mod.fetch_project_record(db_mod.get_conn(c.cfg.workdir), project_id) is None:
        return jsonify({"error": "project not found"}), 404
    body = request.get_json(force=True) or {}
    if "auto_analyze" in body:
        project_mod.set_project_setting(
            c.cfg.workdir, project_id, "auto_analyze", bool(body["auto_analyze"]))
    return jsonify({"ok": True})


# ── 每素材项目状态（片段 / 说话人分段） ──────────────────────

@bp.get("/api/items/<item_id>/project")
def api_project_get(item_id: str) -> object:
    c = ctx()
    c.store.require(item_id)
    return jsonify(project_mod.load_project(c.cfg.workdir, item_id))


@bp.post("/api/items/<item_id>/project")
def api_project_save(item_id: str) -> object:
    c = ctx()
    c.store.require(item_id)
    body = request.get_json(force=True) or {}
    proj = project_mod.load_project(c.cfg.workdir, item_id)
    if "segments" in body:
        proj["segments"] = body["segments"] or []
    if "speaker_segments" in body:
        proj["speaker_segments"] = body["speaker_segments"] or []
    project_mod.save_project(c.cfg.workdir, item_id, proj)
    return jsonify({"ok": True})


@bp.post("/api/items/<item_id>/autosplit")
def api_autosplit(item_id: str) -> object:
    c = ctx()
    c.store.require(item_id)
    body = request.get_json(force=True) or {}
    tid = c.tasks.submit(_autosplit_worker, c, item_id, body)
    return jsonify({"task_id": tid})


def _autosplit_worker(c, item_id: str, body: dict) -> dict:
    tid = c.tasks.current_task_id()
    item = c.store.require(item_id)
    threshold_db = float(body.get("threshold_db") or -35.0)
    min_silence = float(body.get("min_silence") or 0.5)
    min_len = float(body.get("min_len") or 0.8)
    max_len = float(body.get("max_len") or 15.0)
    language = body.get("language") or "JP"
    if tid and c.tasks.cancelled(tid):
        raise TaskCancelled()
    if tid:
        c.tasks.update(tid, progress=0.1, message="detecting silence")
    silences = detect_silence(item.wav_path, threshold_db=threshold_db,
                              min_silence=min_silence)
    duration = item.duration or media_duration(item.wav_path)
    if tid:
        c.tasks.update(tid, progress=0.5, message="splitting by silence")
    clips = autosplit_mod.split_by_silence(duration, silences,
                                           min_len=min_len, max_len=max_len)
    segs = [{
        "id": project_mod.new_uid("seg"),
        "start": round(s, 3), "end": round(e, 3),
        "text": "", "language": language,
        "speakerLabel": "", "characterId": None, "note": "",
    } for s, e in clips]
    proj = project_mod.load_project(c.cfg.workdir, item_id)
    proj["segments"] = segs
    project_mod.save_project(c.cfg.workdir, item_id, proj)
    if tid:
        c.tasks.update(tid, progress=1.0, message="done")
    return {"count": len(segs), "clips": clips}


# ── 说话人识别 ────────────────────────────────────────────

@bp.get("/api/items/<item_id>/speakers")
def api_speakers_get(item_id: str) -> object:
    c = ctx()
    c.store.require(item_id)
    proj = project_mod.load_project(c.cfg.workdir, item_id)
    return jsonify({"speaker_segments": proj["speaker_segments"],
                    "characters": proj["characters"]})


@bp.post("/api/items/<item_id>/speakers/generate")
def api_speakers_generate(item_id: str) -> object:
    c = ctx()
    c.store.require(item_id)
    tid = c.tasks.submit(_speakers_worker, c, item_id, gpu=True, kind="speakers")
    return jsonify({"task_id": tid})


def _speakers_worker(c, item_id: str) -> dict:
    tid = c.tasks.current_task_id()
    item = c.store.require(item_id)
    subs = c.load_subs(item)
    if not subs:
        c.tasks.update(tid, progress=0.05, message="未找到字幕，先执行 Whisper 识别…")
        subs = transcribe_mod.transcribe_timed(
            item.wav_path, language="ja", model="medium",
            progress_cb=lambda p: c.tasks.update(tid, progress=p * 0.3,
                                                 message=f"识别字幕 {p * 100:.0f}%"))
        if subs:
            subs_dir = c.cfg.workdir / "subs"
            subs_dir.mkdir(parents=True, exist_ok=True)
            path = subtitles_mod.write_srt(subs_dir / f"{item.id}.srt", subs)
            item.extra["subs_file"] = str(path)
            c.store.persist(item)
    if not subs:
        raise RuntimeError("未识别到语音内容，无法区分说话人")
    res = speakers_mod.generate_speakers(
        item.wav_path, subs,
        progress_cb=lambda p: c.tasks.update(tid, progress=0.3 + p * 0.6,
                                             message=f"说话人声纹聚类 {p * 100:.0f}%"),
    )
    speaker_segments = res["speaker_segments"]
    project_id = item.project_id or c.default_project_id()
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    label_embeds = res.get("label_embeddings") or {}
    cleaned = 0
    if res.get("quality") != "mfcc" and label_embeds:
        # 清理旧版本产生的“一人一窗口”垃圾角色：只删自动命名(说话人N)、仅归属
        # 本素材、从未合并(emb_count<=1)、且未训练(无 exp)的角色；清空引用它们
        # 的片段绑定，让本次识别重新分配。
        stale = speakers_mod.stale_characters(item.id, pool["characters"])
        stale_ids = {c["id"] for c in stale}
        if stale_ids:
            cleaned = len(stale_ids)
            pool["characters"] = [c for c in pool["characters"] if c["id"] not in stale_ids]
            proj_tmp = project_mod.load_project(c.cfg.workdir, item.id)
            changed = False
            for seg in proj_tmp["segments"]:
                if seg.get("characterId") in stale_ids:
                    seg["characterId"] = None
                    seg["speakerLabel"] = None
                    changed = True
            if changed:
                project_mod.save_project(c.cfg.workdir, item.id, proj_tmp)
        # 记忆强绑定：与角色池已有角色高度相似（且唯一最优）的标签直接归并，
        # 跨次识别同人自动并入同一角色，不再每次从零聚类后仅靠弱匹配。
        strong_assign, chars, _matched = speakers_mod.match_labels_strong(
            item.id, label_embeds, pool["characters"])
        remaining = {lb: e for lb, e in label_embeds.items() if lb not in strong_assign}
        assignments, chars, created = speakers_mod.match_labels_to_pool(
            item.id, remaining, chars)
        merged = max(0, len(assignments) - len(created))
    else:
        # MFCC fallback / no embeddings: one project character per label
        chars = pool["characters"]
        assignments = {}
        created = []
        for lb in sorted({s["label"] for s in speaker_segments if s.get("label")}):
            key = f"{item.id}:{lb}"
            existing = next((c for c in chars if key in (c.get("speakerLabels") or [])), None)
            if existing:
                assignments[lb] = existing["id"]
                continue
            cid = project_mod.new_uid("char")
            chars.append({
                "id": cid, "name": lb, "color": project_mod.next_color(),
                "speakerLabels": [key], "created": time.time(),
            })
            assignments[lb] = cid
            created.append(cid)
        merged = 0
    project_mod.save_pool(c.cfg.workdir, project_id, chars)
    char_of_label = {lb: cid for lb, cid in assignments.items()}
    proj = project_mod.load_project(c.cfg.workdir, item.id)
    # 窗口化声纹分段重新绑定：同一句话含两人时标 mixed 且不自动绑定角色，
    # 避免旧逻辑（每字幕单一声纹）把两人并入同一个角色。
    proj["segments"], mixed_segs = speakers_mod.bind_segments(
        proj["segments"], speaker_segments, char_of_label)
    proj["speaker_segments"] = speaker_segments
    project_mod.save_project(c.cfg.workdir, item.id, proj)
    return {"count": len(speaker_segments), "total": res["total"], "labeled": res["labeled"],
            "mixed": res.get("mixed", 0), "mixed_segments": mixed_segs,
            "n_speakers": res["n_speakers"], "quality": res["quality"],
            "speaker_segments": speaker_segments,
            "characters": chars, "created": created, "merged": merged,
            "cleaned": cleaned}


@bp.post("/api/projects/<project_id>/speakers/generate")
def api_project_speakers_generate(project_id: str) -> object:
    """项目级说话人识别：把项目内全部素材的字幕声纹放在一起联合聚类，
    同一个人跨素材保持同一个角色，而不是每个视频各自聚类后再匹配。"""
    c = ctx()
    if db_mod.fetch_project_record(db_mod.get_conn(c.cfg.workdir), project_id) is None:
        return jsonify({"error": "project not found"}), 404
    tid = submit_project_analyze(c, project_id, force=True)
    return jsonify({"task_id": tid})


def submit_project_analyze(c, project_id: str, *, force: bool = False) -> str | None:
    """提交项目级说话人识别（每项目去重排队）。

    - 非 force（导入链自动触发）受项目设置 ``auto_analyze`` 控制，关闭返回 None。
    - 若该项目已有分析在跑，仅打“需要重跑”标记；当前分析完成后自动再排一次，
      保证批量导入时后完成的素材也被纳入本次分析。
    """
    if not force and not project_mod.get_project_setting(
            c.cfg.workdir, project_id, "auto_analyze", True):
        return None
    with c.auto_analyze_lock:
        existing = c.auto_analyze_tasks.get(project_id)
        if existing:
            t = c.tasks.get(existing)
            if t and t["status"] in ("pending", "running"):
                c.auto_analyze_pending[project_id] = True
                return existing
        tid = c.tasks.submit(_project_speakers_worker, c, project_id,
                             gpu=True, kind="speakers")
        c.auto_analyze_tasks[project_id] = tid
        return tid


def _project_speakers_worker(c, project_id: str) -> dict:
    """项目级说话人识别入口；结束后清理去重注册，必要时自动再排一轮。"""
    tid = c.tasks.current_task_id()
    ok = False
    try:
        result = _project_speakers_run(c, project_id)
        ok = True
        return result
    finally:
        rerun = False
        with c.auto_analyze_lock:
            if c.auto_analyze_tasks.get(project_id) == tid:
                c.auto_analyze_tasks.pop(project_id, None)
            rerun = c.auto_analyze_pending.pop(project_id, False)
        # 仅当本轮分析成功完成且期间又有新导入需要纳入时才再排一轮
        if rerun and ok:
            submit_project_analyze(c, project_id, force=True)


def _project_speakers_run(c, project_id: str) -> dict:
    tid = c.tasks.current_task_id()
    items = [it for it in c.store.by_project(project_id)
             if it.wav_path and Path(it.wav_path).exists()]
    if not items:
        raise RuntimeError("项目内没有可分析的素材")
    # 1) 每个素材准备字幕（缺字幕先 whisper 识别）
    sources: list = []
    for idx, item in enumerate(items):
        if c.tasks.cancelled(tid):
            raise TaskCancelled()
        c.tasks.update(tid, progress=0.05 + 0.2 * idx / len(items),
                       message=f"准备素材 {idx + 1}/{len(items)} …")
        # 后台自动分析可能在排队期间被删素材，单独跳过，不让整轮任务失败
        if not item.wav_path or not Path(item.wav_path).exists():
            continue
        subs = c.load_subs(item)
        if not subs:
            try:
                subs = transcribe_mod.transcribe_timed(
                    item.wav_path, language="ja", model="medium",
                    progress_cb=lambda p: c.tasks.update(tid, progress=p * 0.2,
                                                         message=f"识别字幕 {p * 100:.0f}%"))
            except FileNotFoundError:
                continue  # 素材在识别中途被删除
            if subs:
                subs_dir = c.cfg.workdir / "subs"
                subs_dir.mkdir(parents=True, exist_ok=True)
                path = subtitles_mod.write_srt(subs_dir / f"{item.id}.srt", subs)
                item.extra["subs_file"] = str(path)
                c.store.persist(item)
        if subs:
            sources.append({"item": item, "subs": subs})
    if not sources:
        raise RuntimeError("未识别到语音内容，无法区分说话人")
    # 2) 项目级联合聚类（同一说话人跨素材保持同一标签）
    res = speakers_mod.generate_speakers_project(
        [{"wav_path": s["item"].wav_path, "subs": s["subs"]} for s in sources],
        progress_cb=lambda p: c.tasks.update(tid, progress=0.3 + p * 0.55,
                                             message=f"项目声纹聚类 {p * 100:.0f}%"),
    )
    # 3) 角色池：先清理各素材旧版本垃圾角色，再把统一标签并入项目池
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    chars = pool["characters"]
    cleaned = 0
    stale_ids: set = set()
    for s in sources:
        stale = speakers_mod.stale_characters(s["item"].id, chars)
        stale_ids |= {c["id"] for c in stale}
    if stale_ids:
        cleaned = len(stale_ids)
        chars = [c for c in chars if c["id"] not in stale_ids]
        for it in items:
            proj_tmp = project_mod.load_project(c.cfg.workdir, it.id)
            changed = False
            for seg in proj_tmp["segments"]:
                if seg.get("characterId") in stale_ids:
                    seg["characterId"] = None
                    seg["speakerLabel"] = None
                    changed = True
            if changed:
                project_mod.save_project(c.cfg.workdir, it.id, proj_tmp)
    label_embeds = res.get("label_embeddings") or {}
    if res.get("quality") != "mfcc" and label_embeds:
        # 记忆强绑定：与角色池已有角色高度相似（且唯一最优）的标签直接归并，
        # 跨次识别时同人自动并入同一角色，不再每次从零聚类后仅靠弱匹配。
        strong_assign, chars, _matched = speakers_mod.match_labels_strong(
            project_id, label_embeds, chars)
        remaining = {lb: e for lb, e in label_embeds.items() if lb not in strong_assign}
        weak_assign, chars, created = speakers_mod.match_labels_to_pool(
            project_id, remaining, chars)
        assignments = {**strong_assign, **weak_assign}
        merged = max(0, len(assignments) - len(created))
    else:
        # MFCC fallback / no embeddings: one project character per label
        chars = chars
        assignments = {}
        created = []
        for lb in sorted(label_embeds or {}):
            key = f"{project_id}:{lb}"
            existing = next((c for c in chars if key in (c.get("speakerLabels") or [])), None)
            if existing:
                assignments[lb] = existing["id"]
                continue
            cid = project_mod.new_uid("char")
            chars.append({
                "id": cid, "name": lb, "color": project_mod.next_color(),
                "speakerLabels": [key], "created": time.time(),
            })
            assignments[lb] = cid
            created.append(cid)
        merged = 0
    project_mod.save_pool(c.cfg.workdir, project_id, chars)
    char_of_label = {lb: cid for lb, cid in assignments.items()}
    # 4) 逐素材写回 speaker_segments 并重绑定片段
    total_segs = 0
    total_labeled = 0
    total_mixed = 0
    items_out = []
    for si, s in enumerate(sources):
        if c.tasks.cancelled(tid):
            raise TaskCancelled()
        spk_segs = res["items"][si]["speaker_segments"]
        proj = project_mod.load_project(c.cfg.workdir, s["item"].id)
        proj["segments"], mixed_segs = speakers_mod.bind_segments(
            proj["segments"], spk_segs, char_of_label)
        proj["speaker_segments"] = spk_segs
        project_mod.save_project(c.cfg.workdir, s["item"].id, proj)
        total_segs += len(spk_segs)
        total_labeled += res["items"][si]["labeled"]
        total_mixed += res["items"][si]["mixed"]
        items_out.append({"id": s["item"].id, "count": len(spk_segs),
                          "mixed": res["items"][si]["mixed"]})
    return {"count": total_segs,
            "total": sum(i["total"] for i in res["items"]),
            "labeled": total_labeled, "mixed": total_mixed,
            "n_speakers": res["n_speakers"], "quality": res["quality"],
            "characters": chars, "created": created, "merged": merged,
            "cleaned": cleaned, "items": items_out}

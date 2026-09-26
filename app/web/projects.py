"""项目 / 角色池 / 片段 / 说话人 路由（Blueprint）。"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app import audio_ops as audio_ops_mod
from app import autosplit as autosplit_mod
from app import db as db_mod
from app import gptsovits as gptsovits_mod
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
        "auto_training": bool(project_mod.get_project_setting(
            c.cfg.workdir, project_id, "auto_training", True)),
        "auto_gapscan": bool(project_mod.get_project_setting(
            c.cfg.workdir, project_id, "auto_gapscan", True)),
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
    """项目设置（auto_analyze：导入后自动识别；auto_training：识别后自动训练+试听；
    auto_gapscan：识别完成后自动补扫空白区并归入角色）。"""
    c = ctx()
    if db_mod.fetch_project_record(db_mod.get_conn(c.cfg.workdir), project_id) is None:
        return jsonify({"error": "project not found"}), 404
    body = request.get_json(force=True) or {}
    if "auto_analyze" in body:
        project_mod.set_project_setting(
            c.cfg.workdir, project_id, "auto_analyze", bool(body["auto_analyze"]))
    if "auto_training" in body:
        project_mod.set_project_setting(
            c.cfg.workdir, project_id, "auto_training", bool(body["auto_training"]))
    if "auto_gapscan" in body:
        project_mod.set_project_setting(
            c.cfg.workdir, project_id, "auto_gapscan", bool(body["auto_gapscan"]))
    return jsonify({"ok": True})


# ── 每素材项目状态（片段 / 说话人分段） ──────────────────────

@bp.get("/api/projects/<project_id>/items_state")
def api_project_items_state(project_id: str) -> object:
    """批量拉取项目内全部素材的片段/说话人分段。

    项目切换/自动分析完成后，前端原本对每个素材单发
    /api/items/<id>/project（几十连发）；此端点一次返回全量。
    """
    c = ctx()
    states: dict = {}
    for it in c.store.by_project(project_id):
        proj = project_mod.load_project(c.cfg.workdir, it.id)
        states[it.id] = {"segments": proj["segments"],
                         "speaker_segments": proj["speaker_segments"]}
    return jsonify({"states": states})


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


@bp.post("/api/items/<item_id>/align-speech")
def api_align_speech(item_id: str) -> object:
    """把片段窗口收缩到实际发音区间。

    字幕（内嵌/导入）时间轴是链式的——上句结束即下句开始，是给"阅读"的
    而不是"语音"的边界；直接按字幕切片段会带大量静音。本端点用一次
    silencedetect 检测全文件静音，把每个片段收缩到窗口内的语音区间。
    """
    c = ctx()
    c.store.require(item_id)
    body = request.get_json(force=True, silent=True) or {}
    tid = c.tasks.submit(_align_speech_worker, c, item_id, body)
    return jsonify({"task_id": tid})


def _align_speech_worker(c, item_id: str, body: dict) -> dict:
    tid = c.tasks.current_task_id()
    item = c.store.require(item_id)
    threshold_db = float(body.get("threshold_db") or -35.0)
    min_silence = float(body.get("min_silence") or 0.35)
    if tid and c.tasks.cancelled(tid):
        raise TaskCancelled()
    duration = item.duration or media_duration(item.wav_path)
    if tid:
        c.tasks.update(tid, progress=0.1, message="Silero VAD 检测语音区间 …")
    # 三级检测：Silero 神经 VAD（对 BGM/环境音鲁棒）→ 能量自适应 →
    # 固定电平 silencedetect。任一级得到语音区间即停。
    mode = "silero"
    speech: list[tuple[float, float]] = []
    try:
        speech = audio_ops_mod.detect_speech_ranges(item.wav_path, min_gap=0.30)
    except Exception as exc:  # noqa: BLE001
        if getattr(c, "log", None):
            c.log.warning("silero vad failed: %s", exc)
    if not speech:
        mode = "energy"
        sil = audio_ops_mod.detect_silence_adaptive(item.wav_path,
                                                    min_silence=min(min_silence, 0.30))
        speech = autosplit_mod.speech_ranges(duration, sil)
    if not speech:
        mode = "fixed"
        sil = detect_silence(item.wav_path, threshold_db=threshold_db,
                             min_silence=min_silence)
        speech = autosplit_mod.speech_ranges(duration, sil)
    if tid:
        c.tasks.update(tid, progress=0.5, message=f"对齐片段到发音区间（{mode}）…")
    proj = project_mod.load_project(c.cfg.workdir, item_id)
    segs = proj["segments"]
    changed = autosplit_mod.align_segments_to_speech(segs, duration, speech)
    if changed:
        project_mod.save_project(c.cfg.workdir, item_id, proj)
    if tid:
        c.tasks.update(tid, progress=1.0, message="done")
    return {"ok": True, "changed": changed, "total": len(segs), "mode": mode}


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


def _detach_character_refs(segments: list, ids: set, *, drop_label: bool = False) -> bool:
    """把引用 ``ids`` 中角色的片段解绑，返回是否有改动。

    **locked 段一律跳过**：人工锁定的片段指到哪个角色由用户自己决定，自动清理
    （孤儿角色 / 陈旧角色）不得动它 —— 这是用户现象「改完说话人又被识别改回去」
    的另一半泄漏路径（此前这四处循环只查 characterId，没查 locked）。
    """
    changed = False
    for seg in segments:
        if seg.get("locked"):
            continue
        if seg.get("characterId") in ids:
            seg["characterId"] = None
            if drop_label:
                seg["speakerLabel"] = None
            changed = True
    return changed


def _save_writeback(c, item_id: str, proj: dict) -> dict:
    """识别 / 声纹反馈类任务写回片段的**唯一出口**。

    这些任务是「load_project →（秒~分钟）→ normalize/bind → save_project」的整段
    覆盖写回，而 normalize/bind 判定 ``locked`` 读的是 load 那一刻的快照；用户在
    任务运行期间改好说话人、经前端 400ms 防抖 POST 落盘，就落在 load…save 的窗口
    里被聚类结果覆盖（用户现象「改完说话人又被识别改回去」，有概率）。

    统一走 ``save_project_guarding_locked``：落库前在 db 写锁内重读磁盘，把已
    locked 的人工片段收敛回来。**重新识别前置清理 / 用户显式删除角色等有意清空
    assignment 的路径不要用本函数**（用 project_mod.save_project）。
    """
    return project_mod.save_project_guarding_locked(c.cfg.workdir, item_id, proj)


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
    # 孤儿角色：标签全部指向已删除素材、无训练模型、未被片段引用 → 清掉
    orphan_ids: set = set()
    existing_ids = {it.id for it in c.store.all()}
    pj0 = project_mod.load_project(c.cfg.workdir, item.id)
    referenced = {seg.get("characterId") for seg in pj0["segments"]
                  if seg.get("characterId")}
    orphans = speakers_mod.orphan_characters(
        pool["characters"], existing_ids, referenced)
    if orphans:
        orphan_ids = {x["id"] for x in orphans}
        cleaned += len(orphans)
        pool["characters"] = [x for x in pool["characters"]
                              if x["id"] not in orphan_ids]
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
            if _detach_character_refs(proj_tmp["segments"], stale_ids, drop_label=True):
                _save_writeback(c, item.id, proj_tmp)
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
    # 有字幕但零片段：按字幕行补生成片段，识别结果才有"片段 + 试听"
    if not proj["segments"] and subs:
        fresh = speakers_mod.segments_from_subs(subs)
        for sg in fresh:
            sg["id"] = project_mod.new_uid("s")
        proj["segments"] = fresh
    if orphan_ids:
        _detach_character_refs(proj["segments"], orphan_ids)
    # 窗口化声纹分段重新绑定：同一句话含两人时标 mixed 且不自动绑定角色，
    # 避免旧逻辑（每字幕单一声纹）把两人并入同一个角色。
    # 写回前先去重清理（历史棘轮碎片），重跑识别不再让片段数倍增。
    proj["segments"], norm_removed = speakers_mod.normalize_segments(proj["segments"])
    proj["segments"], mixed_segs = speakers_mod.bind_segments(
        proj["segments"], speaker_segments, char_of_label,
        new_id=project_mod.new_uid)
    proj["speaker_segments"] = speaker_segments
    _save_writeback(c, item.id, proj)
    return {"count": len(speaker_segments), "total": res["total"], "labeled": res["labeled"],
            "mixed": res.get("mixed", 0), "mixed_segments": mixed_segs,
            "normalized_removed": norm_removed,
            "n_speakers": res["n_speakers"], "quality": res["quality"],
            "speaker_segments": speaker_segments,
            "characters": chars, "created": created, "merged": merged,
            "cleaned": cleaned}


@bp.post("/api/projects/<project_id>/speakers/generate")
def api_project_speakers_generate(project_id: str) -> object:
    """项目级说话人识别：把项目内全部素材的字幕声纹放在一起联合聚类，
    同一个人跨素材保持同一个角色，而不是每个视频各自聚类后再匹配。

    body 可选 ``{"reset": true}``：重新识别——先清空项目角色池与全部片段
    指派（从零聚类，不与旧角色归并；片段文本与 locked 标记保留）。
    """
    c = ctx()
    if db_mod.fetch_project_record(db_mod.get_conn(c.cfg.workdir), project_id) is None:
        return jsonify({"error": "project not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    reset = bool(body.get("reset"))
    tid = submit_project_analyze(c, project_id, force=True, reset=reset)
    return jsonify({"task_id": tid})


@bp.post("/api/speakers/feedback")
def api_speakers_feedback() -> object:
    """声纹反馈：用户人工修正的片段作为样本并入角色质心，并静默重匹配
    项目内其他片段（locked 人工锁定片段永不改动）。"""
    c = ctx()
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id")
    samples = body.get("samples") or []
    if not project_id or db_mod.fetch_project_record(
            db_mod.get_conn(c.cfg.workdir), project_id) is None:
        return jsonify({"error": "project not found"}), 404
    clean = []
    for sp in samples:
        try:
            clean.append({"item_id": str(sp["item_id"]), "seg_id": str(sp["seg_id"]),
                          "character_id": str(sp["character_id"]),
                          "start": float(sp["start"]), "end": float(sp["end"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not clean:
        return jsonify({"error": "无有效样本"}), 400
    tid = c.tasks.submit(_speakers_feedback_worker, c, project_id, clean, gpu=True)
    return jsonify({"task_id": tid})


def _merge_pool_fields(c, project_id: str, chars: list,
                       fields: tuple = ("embedding", "emb_count")) -> list:
    """后台任务写池防覆盖：把任务内变更的字段并入**落盘时刻的最新池**再保存。

    声纹反馈 / 空白区补扫等静默任务运行期间用户可继续编辑角色池（改名、
    配色、合并）。若直接保存任务开始时 load 的 chars 快照，会把任务期间
    的用户改名整池回滚（片段列表显示回旧名）。此处改为落盘时刻重新 load
    最新池，只按角色 id 并入本任务拥有的字段（默认 embedding/emb_count），
    其余字段一律以最新池为准；任务期间被删除的角色直接丢弃其声纹更新。
    返回写回后的最新角色列表（供任务 result 使用，前端据此刷新声纹计数）。
    """
    fresh = project_mod.load_pool(c.cfg.workdir, project_id)
    by_id = {ch["id"]: ch for ch in fresh["characters"]}
    for ch in chars:
        dst = by_id.get(ch.get("id"))
        if dst is None:
            continue
        for k in fields:
            if k in ch:
                dst[k] = ch[k]
    project_mod.save_pool(c.cfg.workdir, project_id, fresh["characters"])
    return fresh["characters"]


def _speakers_feedback_worker(c, project_id: str, samples: list) -> dict:
    tid = c.tasks.current_task_id()
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    chars = pool["characters"]
    emb_fn = speakers_mod._pick_embed_fn()
    # 1) 样本声纹并入角色质心（仅收样本段各自的角色）
    n_absorbed = 0
    for sp in samples:
        if not any(ch["id"] == sp["character_id"] for ch in chars):
            continue
        item = c.store.get(sp["item_id"])
        if item is None or not item.wav_path or not Path(item.wav_path).exists():
            continue
        try:
            mono, sr = speakers_mod.read_mono16k(item.wav_path)
            emb = emb_fn(mono, sr, sp["start"], sp["end"])
        except Exception:
            emb = None
        if emb is None:
            continue
        chars = speakers_mod.absorb_character_sample(chars, sp["character_id"], emb)
        n_absorbed += 1
    if not n_absorbed:
        return {"absorbed": 0, "bound": 0, "moved": 0, "characters": chars}
    # 2) 静默重匹配：项目内全部素材的片段逐段重算声纹比对池质心
    #    （识别 worker 同款GPU路径；locked/mixed 段在 rescan 内部跳过）
    items = [it for it in c.store.by_project(project_id)
             if it.wav_path and Path(it.wav_path).exists()]
    sources = []
    for it in items:
        pj = project_mod.load_project(c.cfg.workdir, it.id)
        sources.append({"item_id": it.id, "wav_path": it.wav_path,
                        "segments": pj["segments"]})
    changes, stats = speakers_mod.rescan_assignments(
        sources, chars,
        progress_cb=lambda p: c.tasks.update(tid, progress=0.1 + p * 0.85,
                                             message=f"声纹重匹配 {p * 100:.0f}%"),
        cancelled_cb=lambda: c.tasks.cancelled(tid))
    # 3) 写回（样本段自身用 sample_keys 双保险排除：其 locked 标记可能尚未落盘）
    sample_keys = {(sp["item_id"], sp["seg_id"]) for sp in samples}
    per_item: dict[str, dict[str, str]] = {}
    for (item_id, seg_id), cid in changes.items():
        if (item_id, seg_id) in sample_keys:
            continue
        per_item.setdefault(item_id, {})[seg_id] = cid
    touched = 0
    for it in items:
        segmap = per_item.get(it.id)
        if not segmap:
            continue
        pj = project_mod.load_project(c.cfg.workdir, it.id)
        changed = False
        for seg in pj["segments"]:
            new_cid = segmap.get(seg.get("id"))
            if new_cid and seg.get("characterId") != new_cid and not seg.get("locked"):
                seg["characterId"] = new_cid
                changed = True
        if changed:
            # 上面 546 行的 locked 判定读的是本次 load（时刻 542）的快照：用户此刻
            # 若正在落盘同一素材的人工指派，仍会撞进 542…550 的窗口。统一写回出口
            # 在 db 锁内重读磁盘收敛 locked，把窗口压到 0。
            _save_writeback(c, it.id, pj)
            touched += 1
    # 只并入声纹字段到最新池：任务期间用户的改名/配色不被旧快照回滚
    chars = _merge_pool_fields(c, project_id, chars)
    return {"absorbed": n_absorbed, "bound": stats["bound"], "moved": stats["moved"],
            "scanned": stats["scanned"], "skipped": stats["skipped"],
            "items_touched": touched, "characters": chars}


# ── 空白区补扫（模型自我进化） ──────────────────────────────

@bp.post("/api/speakers/scan-gaps")
def api_speakers_scan_gaps() -> object:
    """扫描“没有任何片段覆盖”的时间区间，自动静默补出遗漏的说话片段。

    流程：现有片段取覆盖补集 → 神经 VAD 求人声区间 → 与补集求交得到候选 →
    逐段提声纹与角色质心比对：高置信（相似度 >= bind_thr 且领先第二名
    >= bind_margin）直接绑定，并把该段吸收进角色质心（模型自我进化）；
    低置信建为未分配片段，等待后续声纹重匹配。全程不改动已有片段。
    """
    c = ctx()
    body = request.get_json(force=True, silent=True) or {}
    project_id = body.get("project_id")
    if not project_id or db_mod.fetch_project_record(
            db_mod.get_conn(c.cfg.workdir), project_id) is None:
        return jsonify({"error": "project not found"}), 404
    tid = c.tasks.submit(
        _speakers_gapscan_worker, c, project_id,
        float(body.get("min_dur") or 0.6), float(body.get("max_dur") or 15.0),
        float(body.get("bind_thr") or 0.80), float(body.get("bind_margin") or 0.05),
        int(body.get("limit") or 40), gpu=True, kind="gapscan")
    return jsonify({"task_id": tid})


def _gap_ranges(segments: list, duration: float, min_gap: float = 0.25) -> list:
    """片段未覆盖的时间区间（补集），忽略过短缝隙。"""
    spans = sorted(
        (float(s.get("start", 0) or 0), float(s.get("end", 0) or 0))
        for s in (segments or [])
        if float(s.get("end", 0) or 0) > float(s.get("start", 0) or 0))
    gaps: list = []
    cur = 0.0
    for st, en in spans:
        if st - cur >= min_gap:
            gaps.append((cur, st))
        cur = max(cur, en)
    if duration - cur >= min_gap:
        gaps.append((cur, duration))
    return [(round(a, 3), round(b, 3)) for a, b in gaps if b > a]


def _clip_ranges(ranges: list, spans: list, min_len: float = 0.05) -> list:
    """两区间列表求交（覆盖补集 ∩ VAD 人声区间）。"""
    out = []
    for a, b in ranges:
        for s, e in spans:
            lo, hi = max(a, s), min(b, e)
            if hi - lo >= min_len:
                out.append((round(lo, 3), round(hi, 3)))
    return out


def _speakers_gapscan_worker(c, project_id: str, min_dur: float, max_dur: float,
                             bind_thr: float, bind_margin: float, limit: int) -> dict:
    import numpy as np

    tid = c.tasks.current_task_id()
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    chars = pool["characters"]
    emb_fn = speakers_mod._pick_embed_fn()

    def _cents(characters: list) -> list:
        out = []
        for ch in characters:
            e = speakers_mod.embedding_from_b64(ch.get("embedding"))
            if e is not None:
                out.append((ch["id"], np.asarray(e, dtype=np.float32)))
        return out

    cents = _cents(chars)
    items = [it for it in c.store.by_project(project_id)
             if it.wav_path and Path(it.wav_path).exists()]
    added: list = []
    bound = pending = 0
    for idx, it in enumerate(items):
        p_n = min(0.92, (idx + 1) / max(1, len(items)) * 0.9)
        c.tasks.update(tid, progress=p_n, message=f"补扫空白区 {idx + 1}/{len(items)}")
        pj = project_mod.load_project(c.cfg.workdir, it.id)
        segs = list(pj.get("segments") or [])
        dur = float(it.duration or audio_ops_mod.wav_duration(it.wav_path) or 0)
        if dur <= 0:
            continue
        gaps = _gap_ranges(segs, dur)
        if not gaps:
            continue
        try:
            speech = audio_ops_mod.detect_speech_ranges(it.wav_path)
        except Exception:
            # Silero 不可用 → 能量自适应静音检测兜底（返回静音区间，取补集即人声）
            speech = autosplit_mod.speech_ranges(
                dur, audio_ops_mod.detect_silence_adaptive(it.wav_path))
        cands = [(a, b) for a, b in _clip_ranges(gaps, speech)
                 if min_dur <= (b - a) <= max_dur]
        if not cands:
            continue
        mono, sr = speakers_mod.read_mono16k(it.wav_path)
        news = []
        for a, b in cands:
            if len(added) + len(news) >= limit:
                break
            if c.tasks.cancelled(tid):
                break
            try:
                emb = emb_fn(mono, sr, a, b)
            except Exception:
                emb = None
            cid = None
            if emb is not None and cents:
                sims = sorted(((c_id, float(speakers_mod._cos(emb, cv)))
                               for c_id, cv in cents), key=lambda x: -x[1])
                best_cid, best_sim = sims[0]
                second = sims[1][1] if len(sims) > 1 else -1.0
                if best_sim >= bind_thr and (best_sim - second) >= bind_margin:
                    cid = best_cid
                    # 吸收样本 → 质心向该角色移动（自我进化），并刷新本地质心表
                    chars = speakers_mod.absorb_character_sample(chars, cid, emb)
                    e2 = next((speakers_mod.embedding_from_b64(x.get("embedding"))
                               for x in chars if x["id"] == cid), None)
                    if e2 is not None:
                        cents = [t for t in cents if t[0] != cid] + \
                                [(cid, np.asarray(e2, dtype=np.float32))]
            news.append({"id": project_mod.new_uid("s"),
                         "start": round(a, 3), "end": round(b, 3), "text": "",
                         "language": "JP", "speakerLabel": None, "characterId": cid,
                         "mixed": False, "origin": "gapscan"})
            if cid:
                bound += 1
            else:
                pending += 1
        if news:
            segs = segs + news
            segs.sort(key=lambda s: float(s.get("start", 0) or 0))
            pj["segments"] = segs
            project_mod.save_project(c.cfg.workdir, it.id, pj)
            added.extend({"item_id": it.id, "id": s["id"],
                          "start": s["start"], "end": s["end"]} for s in news)
    if bound:
        # 只并入声纹字段到最新池：补扫是静默任务，期间用户的改名不被旧快照回滚
        chars = _merge_pool_fields(c, project_id, chars)
    return {"added": len(added), "bound": bound, "pending": pending,
            "items_scanned": len(items), "new_segments": added, "characters": chars}


def submit_project_analyze(c, project_id: str, *, force: bool = False,
                           reset: bool = False) -> str | None:
    """提交项目级说话人识别（每项目去重排队）。

    - 非 force（导入链自动触发）受项目设置 ``auto_analyze`` 控制，关闭返回 None。
    - 若该项目已有分析在跑，仅打“需要重跑”标记；当前分析完成后自动再排一次，
      保证批量导入时后完成的素材也被纳入本次分析（重跑轮按普通识别处理）。
    - ``reset``：重新识别，先清空角色池与全部片段指派再聚类。
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
        tid = c.tasks.submit(_project_speakers_worker, c, project_id, reset,
                             gpu=True, kind="speakers")
        c.auto_analyze_tasks[project_id] = tid
        return tid


def _project_speakers_worker(c, project_id: str, reset: bool = False) -> dict:
    """项目级说话人识别入口；结束后清理去重注册，必要时自动再排一轮。"""
    tid = c.tasks.current_task_id()
    ok = False
    result: dict = {}
    try:
        result = _project_speakers_run(c, project_id, reset=reset)
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
        # 识别成功收尾：自动启动「训练 + 角色试听」管线（项目设置可关）
        if ok:
            try:
                _maybe_auto_pipeline(c, project_id, result)
            except Exception as exc:  # noqa: BLE001
                c.log.warning("auto pipeline submit failed: %s", exc)


def _maybe_auto_pipeline(c, project_id: str, result: dict) -> dict:
    """识别完成后自动为池内每个角色提交「训练 → 生成试听音频」任务。

    - 受项目设置 ``auto_training``（默认开）控制；
    - GPT-SoVITS 未配置时静默跳过；
    - GPU 任务池单 worker，多角色任务天然串行排队，无显存冲突；
    - 已在训练中的角色自动去重。
    返回提交的角色数。
    """
    if not project_mod.get_project_setting(c.cfg.workdir, project_id,
                                           "auto_training", True):
        return 0
    if not (result.get("labeled") or 0) and not result.get("characters"):
        return 0
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    try:
        gptsovits_mod.require_root(settings)
    except Exception:
        c.log.info("auto training skipped: GPT-SoVITS root not configured")
        return 0
    chars = [ch for ch in (result.get("characters")
                           or c.project_pool(project_id))]
    if not chars:
        return 0
    submitted = 0
    for role in chars:
        role_id = role["id"]
        with c.training_lock:
            if role_id in c.training_tasks:
                continue   # 已有训练/试听任务在排队
        try:
            c.tasks.submit(_auto_role_worker, c, project_id, dict(role),
                           gpu=True, kind="train")
            submitted += 1
        except Exception as exc:  # noqa: BLE001
            c.log.warning("auto pipeline role %s submit failed: %s", role_id, exc)
    return submitted


def _auto_role_worker(c, project_id: str, role: dict) -> dict:
    """单角色自动管线：数据量足够则全阶段训练，随后（或跳过训练直接）
    用该角色参考音频合成一段试听，写入角色池供用户听声辨认。"""
    # 延迟导入：training 是独立 Blueprint（app.web.__init__ 里 projects 先注册），
    # 且 _training_worker 是训练交付页的内部实现——这里刻意复用它，使
    # 「自动管线」与「手动点训练」走完全同一条代码路径。模块级导入会形成环。
    from app.web.training import _training_worker
    tid = c.tasks.current_task_id()
    role_id = role["id"]
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    exp = c.role_exp(settings, role)
    trained = False
    try:
        with c.training_lock:
            c.training_tasks[role_id] = tid
        segs, sources, majority_lang = c.role_clips(project_id, role_id)
        lang = gptsovits_mod.lang_map(majority_lang or settings.get("language") or "ja")
        if tid:
            c.tasks.update(tid, message=f"自动管线：{role.get('name')} "
                                        f"({len(segs)} 片段)")
        if len(segs) >= 3:
            stages = {"export": True, "preprocess": True,
                      "train_s2": True, "train_s1": True}
            _training_worker(c, project_id, role, stages,
                             {"exp_name": exp, "language": majority_lang})
            trained = True
        else:
            # 片段太少不足以微调：用底模 zero-shot 合成试听（不训练）
            with c.training_lock:
                c.training_tasks.pop(role_id, None)
        # 生成角色试听音频
        sample = _auto_infer_sample(c, project_id, role, exp, lang)
        return {"ok": True, "role_id": role_id, "role": role.get("name"),
                "exp": exp, "trained": trained, "sample": sample}
    except TaskCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "role_id": role_id, "role": role.get("name"),
                "exp": exp, "trained": trained, "error": str(exc)[:300]}
    finally:
        with c.training_lock:
            if c.training_tasks.get(role_id) == tid:
                c.training_tasks.pop(role_id, None)


def _auto_infer_sample(c, project_id: str, role: dict, exp: str, lang: str) -> dict:
    """为角色合成一段试听音频并把地址写进角色池。"""
    from app.web.training import _pick_ref_clip  # 延迟导入，理由同 _auto_role_worker
    tid = c.tasks.current_task_id()
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    segs, sources, _mj = c.role_clips(project_id, role["id"])
    ref_wav, prompt_text = _pick_ref_clip(segs, sources)
    # 合成文本：优先用该角色最长的台词（最能帮助辨认声线），过长截断
    cand = sorted((s.text.strip() for s in segs if s.text.strip()), key=len,
                  reverse=True)
    text = (cand[0][:48] if cand else "こんにちは。よろしくお願いします。")
    if tid:
        c.tasks.update(tid, message=f"生成试听音频：{role.get('name')}")
    wav = gptsovits_mod.infer(
        settings, exp, text=text, ref_wav=str(ref_wav),
        prompt_text=prompt_text, text_lang=lang, prompt_lang=lang,
        log_cb=lambda s: c.tasks.update(tid, message=(s or "")[:200]) if tid else None)
    out_dir = c.cfg.workdir / "training"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"infer_{gptsovits_mod.sanitize(exp)}.wav"
    path.write_bytes(wav)
    url = f"/api/training/infer-audio/{gptsovits_mod.sanitize(exp)}"
    # 写入角色池（角色任务串行执行，无并发写池冲突）
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    for ch in pool["characters"]:
        if ch["id"] == role["id"]:
            ch["sample_url"] = url
            ch["sample_text"] = text
            ch["sample_at"] = time.time()
    project_mod.save_pool(c.cfg.workdir, project_id, pool["characters"])
    return {"url": url, "text": text, "path": str(path), "ref": str(ref_wav)}


def _reset_project_pool(c, project_id: str, item_ids: list) -> int:
    """重新识别前置清理：清空项目角色池与全部片段的说话人指派。

    片段文本与 locked 标记保留（人工文本成果不受影响），仅解除
    characterId / speakerLabel 绑定，使聚类从零开始、不与旧角色归并。
    返回清掉的角色数。
    """
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    n_chars = len(pool["characters"])
    if n_chars:
        project_mod.save_pool(c.cfg.workdir, project_id, [])
    for iid in item_ids:
        pj = project_mod.load_project(c.cfg.workdir, iid)
        changed = False
        for seg in pj["segments"]:
            if seg.get("characterId"):
                seg["characterId"] = None
                changed = True
            if seg.get("speakerLabel"):
                seg["speakerLabel"] = None
                changed = True
        if changed:
            project_mod.save_project(c.cfg.workdir, iid, pj)
    if getattr(c, "log", None):
        c.log.info("re-identify: cleared %d characters for project %s", n_chars, project_id)
    return n_chars


def _project_speakers_run(c, project_id: str, reset: bool = False) -> dict:
    tid = c.tasks.current_task_id()
    items = [it for it in c.store.by_project(project_id)
             if it.wav_path and Path(it.wav_path).exists()]
    if not items:
        raise RuntimeError("项目内没有可分析的素材")
    reset_chars = 0
    if reset:
        # 重新识别：清空角色池与全部指派（从零聚类，不与旧角色归并）
        c.tasks.update(tid, progress=0.02,
                       message="重新识别：清空旧角色与片段指派 …")
        reset_chars = _reset_project_pool(
            c, project_id, [it.id for it in items])
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
    # 3a) 孤儿角色：标签全部指向已删除素材的角色永远无法再匹配任何片段，
    #     只会污染池子（表现为"识别后角色没有归一"），先清掉。
    existing_ids = {it.id for it in c.store.all()}
    referenced: set = set()
    for it in items:
        pj = project_mod.load_project(c.cfg.workdir, it.id)
        referenced |= {seg.get("characterId") for seg in pj["segments"]
                       if seg.get("characterId")}
    orphans = speakers_mod.orphan_characters(chars, existing_ids, referenced)
    if orphans:
        orphan_ids = {x["id"] for x in orphans}
        cleaned += len(orphans)
        chars = [x for x in chars if x["id"] not in orphan_ids]
        for it in items:
            pj = project_mod.load_project(c.cfg.workdir, it.id)
            if _detach_character_refs(pj["segments"], orphan_ids):
                _save_writeback(c, it.id, pj)
    for s in sources:
        stale = speakers_mod.stale_characters(s["item"].id, chars)
        stale_ids |= {c["id"] for c in stale}
    if stale_ids:
        cleaned = len(stale_ids)
        chars = [c for c in chars if c["id"] not in stale_ids]
        for it in items:
            proj_tmp = project_mod.load_project(c.cfg.workdir, it.id)
            if _detach_character_refs(proj_tmp["segments"], stale_ids, drop_label=True):
                _save_writeback(c, it.id, proj_tmp)
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
    total_norm_removed = 0
    items_out = []
    for si, s in enumerate(sources):
        if c.tasks.cancelled(tid):
            raise TaskCancelled()
        spk_segs = res["items"][si]["speaker_segments"]
        proj = project_mod.load_project(c.cfg.workdir, s["item"].id)
        # 有字幕但零片段的素材：按字幕行补生成片段，识别结果才有
        # "片段 + 试听"可看（bind_segments 只重绑已有片段）。
        if not proj["segments"] and s["subs"]:
            fresh = speakers_mod.segments_from_subs(s["subs"])
            for sg in fresh:
                sg["id"] = project_mod.new_uid("s")
            # 字幕时间轴是链式的（上句结束=下句开始），收缩到实际发音区间；
            # 任一级检测失败不影响识别流程（保留字幕原始边界）
            try:
                _speech = audio_ops_mod.detect_speech_ranges(s["item"].wav_path)
                if not _speech:
                    _sil = detect_silence(s["item"].wav_path, threshold_db=-35.0,
                                          min_silence=0.35)
                    _speech = autosplit_mod.speech_ranges(
                        s["item"].duration or media_duration(s["item"].wav_path), _sil)
                autosplit_mod.align_segments_to_speech(fresh, s["item"].duration, _speech)
            except Exception as exc:  # noqa: BLE001
                if getattr(c, "log", None):
                    c.log.warning("align fresh segments failed: %s", exc)
            proj["segments"] = fresh
        # 写回前先去重清理（历史棘轮碎片），重跑识别不再让片段数倍增
        proj["segments"], norm_removed = speakers_mod.normalize_segments(proj["segments"])
        proj["segments"], mixed_segs = speakers_mod.bind_segments(
            proj["segments"], spk_segs, char_of_label,
            new_id=project_mod.new_uid)
        proj["speaker_segments"] = spk_segs
        _save_writeback(c, s["item"].id, proj)
        total_segs += len(spk_segs)
        total_norm_removed += norm_removed
        total_labeled += res["items"][si]["labeled"]
        total_mixed += res["items"][si]["mixed"]
        items_out.append({"id": s["item"].id, "count": len(spk_segs),
                          "mixed": res["items"][si]["mixed"]})
    return {"count": total_segs,
            "total": sum(i["total"] for i in res["items"]),
            "labeled": total_labeled, "mixed": total_mixed,
            "normalized_removed": total_norm_removed,
            "n_speakers": res["n_speakers"], "quality": res["quality"],
            "characters": chars, "created": created, "merged": merged,
            "cleaned": cleaned, "items": items_out,
            "reset": bool(reset), "reset_chars": reset_chars}

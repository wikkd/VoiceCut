"""GPT-SoVITS 训练交付路由（Blueprint）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from app import gptsovits as gptsovits_mod
from app import project as project_mod
from app.web.context import TRAINING_STATUS_TTL

bp = Blueprint("training", __name__)


def ctx():
    return current_app.extensions["vc_ctx"]


@bp.get("/api/training/config")
def api_training_config_get() -> object:
    return jsonify({"settings": gptsovits_mod.load_settings(ctx().cfg.workdir)})


@bp.post("/api/training/config")
def api_training_config_save() -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    data = body.get("settings") or body
    try:
        merged = gptsovits_mod.save_settings(c.cfg.workdir, data)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    return jsonify({"settings": merged})


@bp.get("/api/training/status")
def api_training_status() -> object:
    c = ctx()
    project_id = request.args.get("project_id") or c.default_project_id()
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    _sig = json.dumps({
        "pid": project_id,
        "settings": settings,
        "training": dict(c.training_tasks),
        "running": [(t["id"], t["status"]) for t in c.tasks.all()],
    }, sort_keys=True, default=str)
    now = time.time()
    if (c.training_status_cache["key"] == _sig
            and now - c.training_status_cache["ts"] < TRAINING_STATUS_TTL
            and c.training_status_cache["data"] is not None):
        return jsonify(c.training_status_cache["data"])
    roles = []
    for ch in c.project_pool(project_id):
        try:
            segs, _sources, majority_lang = c.role_clips(project_id, ch["id"])
            total_dur = sum(float(s.end) - float(s.start) for s in segs)
            exp = c.role_exp(settings, ch)
            edir = gptsovits_mod.exp_dir(settings, exp)
            list_lines = 0
            wavs = 0
            clips: list[dict] = []
            if (edir / "list.txt").exists():
                lines = [ln for ln in (edir / "list.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
                list_lines = len(lines)
                for ln in lines[:200]:
                    parts = ln.split("|")
                    if len(parts) >= 4:
                        clips.append({"wav": Path(parts[0]).name, "text": parts[3].strip()})
            if edir.exists():
                wavs = len(list(edir.glob("*.wav")))
            preprocessed = (edir / "2-name2text.txt").exists() and (edir / "6-name2semantic.tsv").exists()
            weights = {"gpt": None, "sovits": None}
            if edir.exists():
                weights = gptsovits_mod.discover_weights(settings, exp)
            roles.append({
                "id": ch["id"], "name": ch.get("name") or "未命名",
                "color": ch.get("color") or "#888888",
                "clips": len(segs), "duration": round(total_dur, 1),
                "language": majority_lang, "exp": exp,
                "dataset": {"exists": edir.exists(), "wavs": wavs,
                            "list_lines": list_lines,
                            "preprocessed": bool(preprocessed),
                            "dir": str(edir) if edir.exists() else None,
                            "clips": clips},
                "weights": weights,
                "pipeline": {"running": ch["id"] in c.training_tasks,
                             "task_id": c.training_tasks.get(ch["id"]) or None},
            })
        except Exception as exc:  # noqa: BLE001
            c.log.warning("training status role %s failed: %s", ch.get("id"), exc)
    data = {
        "ok": True, "project_id": project_id, "settings": settings,
        "api": {"running": gptsovits_mod.api_running(settings),
                "port": int(settings.get("api_port") or 9880)},
        "gpu_busy": any(t["status"] == "running" for t in c.tasks.all()),
        "roles": roles,
    }
    c.training_status_cache["key"] = _sig
    c.training_status_cache["ts"] = now
    c.training_status_cache["data"] = data
    return jsonify(data)


@bp.get("/api/training/weights")
def api_training_weights() -> object:
    c = ctx()
    project_id = request.args.get("project_id") or c.default_project_id()
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    out = []
    for ch in c.project_pool(project_id):
        exp = c.role_exp(settings, ch)
        w = {"gpt": None, "sovits": None}
        if gptsovits_mod.exp_dir(settings, exp).exists():
            w = gptsovits_mod.discover_weights(settings, exp)
        out.append({"role_id": ch["id"], "name": ch.get("name"), "exp": exp, "weights": w})
    return jsonify(out)


@bp.post("/api/training/roles/<role_id>/pipeline")
def api_training_pipeline(role_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id") or request.args.get("project_id") or c.default_project_id()
    stages = {
        "export": bool(body.get("export", True)),
        "preprocess": bool(body.get("preprocess", True)),
        "train_s2": bool(body.get("train_s2", True)),
        "train_s1": bool(body.get("train_s1", True)),
    }
    return _submit_training(c, project_id, role_id, stages, body)


@bp.post("/api/training/roles/<role_id>/preprocess")
def api_training_preprocess(role_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id") or request.args.get("project_id") or c.default_project_id()
    stages = {"export": bool(body.get("export", True)), "preprocess": True,
              "train_s2": False, "train_s1": False}
    return _submit_training(c, project_id, role_id, stages, body)


@bp.post("/api/training/roles/<role_id>/train-s2")
def api_training_s2(role_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id") or request.args.get("project_id") or c.default_project_id()
    stages = {"export": False, "preprocess": False, "train_s2": True, "train_s1": False}
    return _submit_training(c, project_id, role_id, stages, body)


@bp.post("/api/training/roles/<role_id>/train-s1")
def api_training_s1(role_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id") or request.args.get("project_id") or c.default_project_id()
    stages = {"export": False, "preprocess": False, "train_s2": False, "train_s1": True}
    return _submit_training(c, project_id, role_id, stages, body)


@bp.get("/api/training/roles/<role_id>/logs")
def api_training_logs(role_id: str) -> object:
    c = ctx()
    exp = request.args.get("exp") or ""
    task_id = request.args.get("task") or ""
    path = None
    cands = []
    if exp:
        cands.append(c.cfg.workdir / "training" / f"{gptsovits_mod.sanitize(exp)}.log")
    if task_id:
        cands.append(c.cfg.workdir / "training" / f"task_{task_id}.log")
    for cand in cands:
        if cand.exists():
            path = cand
            break
    if path is None:
        g = sorted((c.cfg.workdir / "training").glob("*.log"),
                   key=lambda p: p.stat().st_mtime, reverse=True) \
            if (c.cfg.workdir / "training").exists() else []
        path = g[0] if g else None
    tail = ""
    if path:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            tail = "\n".join(lines[-400:])
        except Exception:  # noqa: BLE001
            tail = ""
    return jsonify({"logs": tail, "path": str(path) if path else None})


def _pick_ref_clip(segs: list, sources: dict) -> tuple:
    if not segs:
        raise RuntimeError("该角色没有可用片段可作参考")
    best = segs[0]
    best_score = float("inf")
    for s in segs:
        d = float(s.end) - float(s.start)
        score = abs(d - 4.0) if 2.0 <= d <= 8.0 else 1000.0 + abs(d - 4.0)
        if score < best_score:
            best_score = score
            best = s
    return sources[best.item_id], best.text


def _submit_training(c, project_id: str, role_id: str, stages: dict, body: dict) -> object:
    role = next((ch for ch in c.project_pool(project_id) if ch["id"] == role_id), None)
    if not role:
        return jsonify({"error": "角色不存在"}), 404
    with c.training_lock:
        if role_id in c.training_tasks:
            return jsonify({"error": "该角色已有训练任务在运行"}), 409
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    exp = c.role_exp(settings, role, body.get("exp_name") or "")
    # 记录 exp 到角色池，便于重训复用
    pool = project_mod.load_pool(c.cfg.workdir, project_id)
    for ch in pool["characters"]:
        if ch["id"] == role_id:
            ch["exp"] = exp
    project_mod.save_pool(c.cfg.workdir, project_id, pool["characters"])
    if not any(stages.values()):
        return jsonify({"error": "至少需要一个阶段"}), 400
    with c.training_lock:
        c.training_tasks[role_id] = "pending"
    try:
        tid = c.tasks.submit(_training_worker, c, project_id, role, stages,
                             {**body, "exp_name": exp}, gpu=True)
    except Exception:
        with c.training_lock:
            c.training_tasks.pop(role_id, None)
        raise
    with c.training_lock:
        c.training_tasks[role_id] = tid
    return jsonify({"task_id": tid, "exp": exp, "role_id": role_id})


def _training_worker(c, project_id: str, role: dict, stages: dict, opts: dict) -> dict:
    tid = c.tasks.current_task_id()
    role_id = role["id"]
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    gptsovits_mod.stop_api()  # 释放显存给训练
    exp = opts.get("exp_name") or c.role_exp(settings, role)
    lang = gptsovits_mod.lang_map(opts.get("language") or settings.get("language") or "ja")
    log_dir = c.cfg.workdir / "training"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{exp}.log"
    log_file = open(log_path, "a", encoding="utf-8")
    try:
        def _log(line: str) -> None:
            try:
                log_file.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
                log_file.flush()
            except Exception:  # noqa: BLE001
                pass
            if tid:
                c.tasks.update(tid, message=(line or "")[:200])

        def _cancel() -> bool:
            return bool(tid and c.tasks.cancelled(tid))

        def _prog(start: float, span: float):
            def _cb(p: float) -> None:
                if tid:
                    c.tasks.update(tid, progress=start + span * max(0.0, min(1.0, p)))
            return _cb

        _log(f"开始训练管线：角色={role.get('name')} exp={exp} lang={lang}")
        segs, sources, majority_lang = c.role_clips(project_id, role_id)
        lang = gptsovits_mod.lang_map(opts.get("language") or majority_lang)
        if not segs:
            raise RuntimeError("该角色没有可用的有效片段（需有文本且时长≥0.5s）")
        total_dur = sum(float(s.end) - float(s.start) for s in segs)
        if stages.get("export", True):
            if tid:
                c.tasks.update(tid, progress=0.03, message="导出数据集…")
            _log(f"① 导出数据集：{len(segs)} 片段 / {total_dur:.1f}s")
            ds = gptsovits_mod.build_dataset(
                settings, exp, segs, sources,
                speaker=role.get("name") or exp, language=lang,
                val_ratio=float(opts.get("val_ratio") or settings.get("val_ratio") or 0),
                tasks=c.tasks, task_id=tid)
            _log("导出完成：%d 条写入 %s" % (ds.get("count"), ds.get("out_dir")))
            if ds.get("val_holdout"):
                _log("预留验证集 %d 条" % ds.get("val_holdout"))
        if stages.get("preprocess", True):
            if tid:
                c.tasks.update(tid, progress=0.10, message="预处理（文本/SSL/语义）…")
            gptsovits_mod.run_preprocess(
                settings, exp, version=settings.get("version") or "v2",
                log_cb=_log, cancelled_cb=_cancel,
                start_progress=0.10, span=0.20, progress_cb=_prog(0.10, 0.20))
        gptsovits_mod.write_training_configs(
            settings, exp,
            epochs_s1=int(opts["epochs_s1"]) if opts.get("epochs_s1") else None,
            epochs_s2=int(opts["epochs_s2"]) if opts.get("epochs_s2") else None)
        if stages.get("train_s2", True):
            if tid:
                c.tasks.update(tid, progress=0.35, message="SoVITS(S2) 训练…")
            _log("④ SoVITS(S2) 训练开始")
            gptsovits_mod.run_training(settings, "s2", log_cb=_log, cancelled_cb=_cancel)
            if tid:
                c.tasks.update(tid, progress=0.62, message="SoVITS(S2) 完成")
        if stages.get("train_s1", True):
            if tid:
                c.tasks.update(tid, progress=0.66, message="GPT(S1) 训练…")
            _log("⑤ GPT(S1) 训练开始")
            gptsovits_mod.run_training(settings, "s1", log_cb=_log, cancelled_cb=_cancel)
            if tid:
                c.tasks.update(tid, progress=0.95, message="GPT(S1) 完成")
        weights = gptsovits_mod.discover_weights(settings, exp)
        _log("训练完成 exp=%s；权重 GPT=%s SoVITS=%s" % (exp, weights.get("gpt"), weights.get("sovits")))
        return {
            "ok": True, "role_id": role_id, "exp": exp,
            "dataset": str(gptsovits_mod.exp_dir(settings, exp)),
            "weights": weights, "log_file": str(log_path),
            "count": len(segs),
        }
    finally:
        with c.training_lock:
            if c.training_tasks.get(role_id) == tid:
                c.training_tasks.pop(role_id, None)
        log_file.close()


@bp.post("/api/training/roles/<role_id>/infer")
def api_training_infer(role_id: str) -> object:
    c = ctx()
    body = request.get_json(force=True) or {}
    project_id = body.get("project_id") or request.args.get("project_id") or c.default_project_id()
    role = next((ch for ch in c.project_pool(project_id) if ch["id"] == role_id), None)
    if not role:
        return jsonify({"error": "角色不存在"}), 404
    with c.training_lock:
        if role_id in c.training_tasks:
            return jsonify({"error": "训练进行中，请结束后再试听（显存冲突）"}), 409
    settings = gptsovits_mod.load_settings(c.cfg.workdir)
    exp = body.get("exp_name") or c.role_exp(settings, role)
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "合成文本为空"}), 400
    lang = gptsovits_mod.lang_map(body.get("language") or settings.get("language") or "ja")
    ref_wav = None
    prompt_text = ""
    ref_name = body.get("ref_name")
    if ref_name:
        edir = gptsovits_mod.exp_dir(settings, exp)
        list_file = edir / "list.txt"
        if list_file.exists():
            for ln in list_file.read_text(encoding="utf-8").splitlines():
                parts = ln.split("|")
                if len(parts) >= 4 and Path(parts[0]).name == ref_name:
                    cand = Path(parts[0])
                    ref_wav = cand if cand.exists() else (edir / ref_name)
                    prompt_text = parts[3].strip()
                    break
    if ref_wav is None:
        segs, sources, _mj = c.role_clips(project_id, role_id)
        ref_wav, prompt_text = _pick_ref_clip(segs, sources)
    out_dir = c.cfg.workdir / "training"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / f"{exp}.infer.log"

    def _log(line: str) -> None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
        except Exception:  # noqa: BLE001
            pass

    try:
        wav = gptsovits_mod.infer(
            settings, exp, text=text, ref_wav=str(ref_wav),
            prompt_text=prompt_text, text_lang=lang, prompt_lang=lang,
            log_cb=_log)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    result_path = out_dir / f"infer_{exp}.wav"
    result_path.write_bytes(wav)
    return jsonify({"audio_url": f"/api/training/infer-audio/{gptsovits_mod.sanitize(exp)}",
                    "path": str(result_path), "ref": str(ref_wav)})


@bp.get("/api/training/infer-audio/<exp>")
def api_training_infer_audio(exp: str) -> object:
    c = ctx()
    safe = gptsovits_mod.sanitize(exp)
    p = c.cfg.workdir / "training" / f"infer_{safe}.wav"
    if not p.exists():
        return jsonify({"error": "未找到合成音频"}), 404
    return send_from_directory(p.parent, p.name, mimetype="audio/wav")

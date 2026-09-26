"""自动训练管线（识别 → 训练 → 试听）回归测试。

背景：``app/web/projects.py`` 的 ``_maybe_auto_pipeline`` / ``_auto_role_worker`` /
``_auto_infer_sample`` 里用到 ``gptsovits_mod`` / ``_training_worker`` /
``_pick_ref_clip`` 三个名字，但它们**从未被导入**。于是 ``_maybe_auto_pipeline``
在 ``gptsovits_mod.load_settings`` 第一行就抛 ``NameError``，被
``_project_speakers_worker`` 的 ``except Exception`` 吞成一条 warning：

    auto pipeline submit failed: name 'gptsovits_mod' is not defined

整条「识别完自动训练 + 生成试听」的功能因此**从未真正执行过**，而 CI 只跑
``ruff check`` + ``pytest``，两者都没覆盖到这条路径。本文件的断言直接调用
这三个函数，任何名字缺失都会以 ``NameError`` 让测试立刻失败。
"""
from __future__ import annotations

import threading
import types
from pathlib import Path

from app.web import projects as P
from app.web import training as T


class _FakeTasks:
    def __init__(self) -> None:
        self.submitted: list[dict] = []

    def submit(self, fn, *args, **kwargs) -> str:
        self.submitted.append({"fn": fn.__name__, "args": args, "kw": kwargs})
        return f"t{len(self.submitted)}"

    def current_task_id(self) -> str:
        return "tid-fake"

    def update(self, *args, **kwargs) -> None:
        pass


class _FakeLog:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, *args, **kwargs) -> None:
        pass

    def warning(self, msg, *args, **kwargs) -> None:
        self.warnings.append(msg % args if args else msg)


class _FakeCtx:
    """只实现自动管线用到的那几个成员，避免拉起真实 GPU/模型依赖。"""

    def __init__(self, workdir: Path, pool: list[dict] | None = None) -> None:
        self.cfg = types.SimpleNamespace(workdir=workdir)
        self.log = _FakeLog()
        self.tasks = _FakeTasks()
        self.training_lock = threading.Lock()
        self.training_tasks: dict[str, str] = {}
        self._pool = pool or []

    def project_pool(self, project_id: str) -> list[dict]:
        return self._pool

    def role_exp(self, settings: dict, role: dict, override: str = "") -> str:
        return f"{role['name']}_exp"


def _patch_root_ok(monkeypatch) -> None:
    monkeypatch.setattr(P.gptsovits_mod, "load_settings", lambda wd: {"root": "/fake"})
    monkeypatch.setattr(P.gptsovits_mod, "require_root", lambda settings: Path("/fake"))


# ── _maybe_auto_pipeline：按角色逐个提交 train 任务 ──────────────


def test_maybe_auto_pipeline_submits_one_train_task_per_role(tmp_path, monkeypatch) -> None:
    """回归：此前这里第一行就 NameError。现在应逐角色提交 kind='train' 任务。"""
    _patch_root_ok(monkeypatch)
    pool = [{"id": "c1", "name": "初乙"}, {"id": "c2", "name": "老板"}]
    c = _FakeCtx(tmp_path, pool)

    n = P._maybe_auto_pipeline(c, "p1", {"labeled": 5})

    assert n == 2
    assert [s["fn"] for s in c.tasks.submitted] == ["_auto_role_worker", "_auto_role_worker"]
    assert all(s["kw"].get("kind") == "train" for s in c.tasks.submitted)
    assert c.log.warnings == []          # 不允许再出现「auto pipeline submit failed」


def test_maybe_auto_pipeline_skips_when_setting_off(tmp_path, monkeypatch) -> None:
    """auto_training 关闭 → 不提交任何任务（也不该因为缺名字而报错）。"""
    _patch_root_ok(monkeypatch)
    monkeypatch.setattr(P.project_mod, "get_project_setting",
                        lambda wd, pid, key, default=None: False)
    c = _FakeCtx(tmp_path, [{"id": "c1", "name": "A"}])

    assert P._maybe_auto_pipeline(c, "p1", {"labeled": 5}) == 0
    assert c.tasks.submitted == []


def test_maybe_auto_pipeline_skips_when_root_unconfigured(tmp_path, monkeypatch) -> None:
    """GPT-SoVITS 未配置 → 静默跳过，不是异常。"""
    monkeypatch.setattr(P.gptsovits_mod, "load_settings", lambda wd: {})

    def _boom(settings):
        raise RuntimeError("root not configured")

    monkeypatch.setattr(P.gptsovits_mod, "require_root", _boom)
    c = _FakeCtx(tmp_path, [{"id": "c1", "name": "A"}])

    assert P._maybe_auto_pipeline(c, "p1", {"labeled": 5}) == 0
    assert c.tasks.submitted == []


def test_maybe_auto_pipeline_dedupes_inflight_role(tmp_path, monkeypatch) -> None:
    """已在训练中的角色跳过（training_tasks 是去重表）。"""
    _patch_root_ok(monkeypatch)
    pool = [{"id": "c1", "name": "A"}, {"id": "c2", "name": "B"}]
    c = _FakeCtx(tmp_path, pool)
    c.training_tasks["c1"] = "running"

    assert P._maybe_auto_pipeline(c, "p1", {"labeled": 5}) == 1
    assert c.tasks.submitted[0]["args"][2]["id"] == "c2"


# ── _auto_role_worker：数据够 → 训练；随后都要合成试听 ───────────


def test_auto_role_worker_trains_when_enough_clips(tmp_path, monkeypatch) -> None:
    """回归：>=3 片段时走到 _training_worker —— 该名字此前未导入。"""
    _patch_root_ok(monkeypatch)
    monkeypatch.setattr(P.gptsovits_mod, "lang_map", lambda code: "ja")

    segs = [types.SimpleNamespace(text=f"台词{i}", start=float(i), end=i + 1.0)
            for i in range(3)]
    c = _FakeCtx(tmp_path)
    c.role_clips = lambda pid, rid: (segs, {"m": "/x.wav"}, "JP")

    stages_seen: list[dict] = []
    monkeypatch.setattr(T, "_training_worker",
                        lambda cc, pid, role, stages, opts: stages_seen.append(stages) or {"ok": True})
    monkeypatch.setattr(P, "_auto_infer_sample",
                        lambda *a, **kw: {"url": "/u.wav", "text": "hi"})

    out = P._auto_role_worker(c, "p1", {"id": "c1", "name": "A"})

    assert out["trained"] is True and out["sample"]["url"] == "/u.wav"
    assert stages_seen and stages_seen[0]["train_s2"] is True
    assert c.training_tasks == {}        # finally 里清掉占位


def test_auto_role_worker_zero_shot_when_too_few_clips(tmp_path, monkeypatch) -> None:
    """<3 片段 → 跳过训练，仍走 zero-shot 试听。"""
    _patch_root_ok(monkeypatch)
    monkeypatch.setattr(P.gptsovits_mod, "lang_map", lambda code: "ja")

    segs = [types.SimpleNamespace(text="就一句", start=0.0, end=1.0)]
    c = _FakeCtx(tmp_path)
    c.role_clips = lambda pid, rid: (segs, {"m": "/x.wav"}, "JP")

    trained: list = []
    monkeypatch.setattr(T, "_training_worker", lambda *a, **kw: trained.append(1))
    monkeypatch.setattr(P, "_auto_infer_sample", lambda *a, **kw: {"url": "/u.wav"})

    out = P._auto_role_worker(c, "p1", {"id": "c1", "name": "A"})

    assert out["trained"] is False and trained == []


def test_auto_role_worker_reports_error_without_raising(tmp_path, monkeypatch) -> None:
    """单角色失败不能炸掉 worker（其余角色还要跑）。"""
    _patch_root_ok(monkeypatch)
    monkeypatch.setattr(P.gptsovits_mod, "lang_map", lambda code: "ja")
    c = _FakeCtx(tmp_path)
    c.role_clips = lambda pid, rid: (_ for _ in ()).throw(RuntimeError("no clips"))

    out = P._auto_role_worker(c, "p1", {"id": "c1", "name": "A"})

    assert out["ok"] is False and "no clips" in out["error"]


# ── _auto_infer_sample：合成音频 + 写回角色池 ───────────────────


def test_auto_infer_sample_writes_back_to_pool(tmp_path, monkeypatch) -> None:
    """回归：_pick_ref_clip 此前未导入；且结果必须落到角色池的 sample_* 字段。"""
    monkeypatch.setattr(P.gptsovits_mod, "load_settings", lambda wd: {})
    monkeypatch.setattr(P.gptsovits_mod, "sanitize", lambda name: name)
    monkeypatch.setattr(P.gptsovits_mod, "infer",
                        lambda settings, exp, **kw: b"RIFF-fake-wav")

    ref = tmp_path / "ref.wav"
    monkeypatch.setattr(T, "_pick_ref_clip",
                        lambda segs, sources: (ref, "プロンプト文"))

    segs = [types.SimpleNamespace(text="短", start=0.0, end=1.0),
            types.SimpleNamespace(text="这是一句明显更长的台词用来做试听合成", start=1.0, end=3.0)]
    c = _FakeCtx(tmp_path)
    c.role_clips = lambda pid, rid: (segs, {"m": "/x.wav"}, "ZH")

    # 真实落盘角色池（save_pool 只 UPDATE 已存在的项目记录，故先建项目）
    P.db_mod.insert_project(P.db_mod.get_conn(tmp_path), "p1", "P1", 0.0, 0.0,
                            {"characters": [{"id": "c1", "name": "A", "color": "#fff"}]})

    out = P._auto_infer_sample(c, "p1", {"id": "c1", "name": "A"}, "A_exp", "zh")

    assert Path(out["path"]).read_bytes() == b"RIFF-fake-wav"
    assert out["url"] == "/api/training/infer-audio/A_exp"
    assert out["text"] == segs[1].text          # 取最长台词

    pool = P.project_mod.load_pool(tmp_path, "p1")
    ch = pool["characters"][0]
    assert ch["sample_url"] == out["url"] and ch["sample_text"] == out["text"]

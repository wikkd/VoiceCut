"""后台任务写池防覆盖测试：静默任务（声纹反馈/补扫）结束时的整池写回
不得把任务期间的用户改名回滚（片段列表显示回旧名的根因）。"""
from __future__ import annotations

from pathlib import Path

from app import db
from app import project as pr
from app.config import AppConfig
from app.media_store import MediaStore
from app.tasks import TaskManager
from app.web import projects as projects_bp
from app.web.context import WebContext


def _mkctx(tmp_path: Path):
    cfg = AppConfig(workdir=tmp_path)
    store = MediaStore(cfg.workdir)
    tm = TaskManager(gpu_workers=1, cpu_workers=1)
    return cfg, WebContext(cfg, store, tm, None)


def test_merge_preserves_rename_and_applies_embedding(tmp_path: Path) -> None:
    """任务期间用户改名并已落盘 → 合并后新名字保留、声纹字段更新。"""
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    pr.save_pool(cfg.workdir, pid, [
        {"id": "c1", "name": "说话人1", "emb_count": 1,
         "embedding": "old-emb"},
    ])
    # 任务开始时的旧快照（旧名）
    worker_chars = [{"id": "c1", "name": "说话人1", "emb_count": 1,
                     "embedding": "old-emb"}]
    # 任务期间：用户改名并保存到服务端
    pr.save_pool(cfg.workdir, pid, [
        {"id": "c1", "name": "鹿目圆", "emb_count": 1,
         "embedding": "old-emb"},
    ])
    # 任务内吸收样本 → embedding/emb_count 变化
    worker_chars[0]["embedding"] = "new-emb"
    worker_chars[0]["emb_count"] = 7

    merged = projects_bp._merge_pool_fields(c, pid, worker_chars)

    assert merged[0]["name"] == "鹿目圆"          # 改名不被回滚
    assert merged[0]["embedding"] == "new-emb"    # 声纹更新生效
    assert merged[0]["emb_count"] == 7
    # 落盘值同样正确
    saved = pr.load_pool(cfg.workdir, pid)["characters"]
    assert saved[0]["name"] == "鹿目圆"
    assert saved[0]["embedding"] == "new-emb"


def test_merge_drops_updates_for_deleted_character(tmp_path: Path) -> None:
    """任务期间角色被用户删除 → 该角色的声纹更新被丢弃，不报错。"""
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    pr.save_pool(cfg.workdir, pid, [{"id": "c1", "name": "甲"}])
    worker_chars = [{"id": "c1", "name": "甲", "emb_count": 3,
                     "embedding": "zombie-emb"}]
    # 任务期间用户删了 c1、新建了 c2
    pr.save_pool(cfg.workdir, pid, [{"id": "c2", "name": "新角色"}])

    merged = projects_bp._merge_pool_fields(c, pid, worker_chars)

    assert [ch["id"] for ch in merged] == ["c2"]
    assert pr.load_pool(cfg.workdir, pid)["characters"][0]["id"] == "c2"


def test_merge_keeps_new_user_character_untouched(tmp_path: Path) -> None:
    """任务期间用户新建的角色不出现在任务快照里 → 不受影响原样保留。"""
    cfg, c = _mkctx(tmp_path)
    pid = db.ensure_default_project(db.get_conn(cfg.workdir))
    pr.save_pool(cfg.workdir, pid, [{"id": "c1", "name": "甲"}])
    worker_chars = [{"id": "c1", "name": "甲", "emb_count": 2,
                     "embedding": "e1"}]
    pr.save_pool(cfg.workdir, pid, [
        {"id": "c1", "name": "甲"},
        {"id": "c2", "name": "乙", "emb_count": 0},
    ])

    merged = projects_bp._merge_pool_fields(c, pid, worker_chars)

    by_id = {ch["id"]: ch for ch in merged}
    assert by_id["c1"]["embedding"] == "e1" and by_id["c1"]["emb_count"] == 2
    assert by_id["c2"]["name"] == "乙" and "embedding" not in by_id["c2"]

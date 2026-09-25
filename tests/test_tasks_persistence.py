"""TaskManager 持久化（tasks 表）与任务链（depends_on）测试。"""
from __future__ import annotations

import time

import pytest

from app import db as db_mod
from app.tasks import TaskManager


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "wd"
    d.mkdir()
    return d


@pytest.fixture()
def conn(workdir):
    c = db_mod.get_conn(workdir)
    yield c
    db_mod.reset_conns()


def _wait(tm: TaskManager, task_id: str, status: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tm.get(task_id)
        if t and t["status"] == status:
            return t
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} 未在 {timeout}s 内进入 {status}: {t}")


# ── 持久化 ──────────────────────────────────────────────────

def test_done_task_persisted_and_readable_after_restart(conn) -> None:
    tm = TaskManager(conn=conn)
    tid = tm.submit(lambda: {"answer": 42})
    done = _wait(tm, tid, "done")
    assert done["result"] == {"answer": 42}

    # 模拟重启：新管理器（内存为空）从 DB 读回
    tm2 = TaskManager(conn=conn)
    t = tm2.get(tid)
    assert t is not None
    assert t["status"] == "done"
    assert t["result"] == {"answer": 42}


def test_restart_marks_running_as_interrupted(conn) -> None:
    tm = TaskManager(conn=conn)

    def slow():
        time.sleep(2.0)

    tid = tm.submit(slow)
    # 等它进入 running 再“重启”
    _wait_running(tm, tid)
    tm2 = TaskManager(conn=conn)
    t = tm2.get(tid)
    assert t["status"] == "interrupted"
    assert "重启" in (t["message"] or "")


def _wait_running(tm: TaskManager, task_id: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = tm.get(task_id)
        if t and t["status"] == "running":
            return
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} 未进入 running")


def test_all_merges_db_history(conn) -> None:
    tm = TaskManager(conn=conn)
    tid = tm.submit(lambda: 1)
    _wait(tm, tid, "done")
    tm2 = TaskManager(conn=conn)
    ids = [t["id"] for t in tm2.all()]
    assert tid in ids
    row = next(t for t in tm2.all() if t["id"] == tid)
    assert row["status"] == "done"
    assert row["result"] == 1


def test_error_task_persisted(conn) -> None:
    tm = TaskManager(conn=conn)

    def boom():
        raise ValueError("坏掉了")

    tid = tm.submit(boom)
    t = _wait(tm, tid, "error")
    assert "坏掉了" in t["message"]

    tm2 = TaskManager(conn=conn)
    assert tm2.get(tid)["status"] == "error"


# ── 任务链（depends_on）─────────────────────────────────────

def test_chain_runs_after_upstream_with_result(conn) -> None:
    tm = TaskManager(conn=conn, cpu_workers=1)
    up = tm.submit(lambda: 5)
    down = tm.submit(lambda r, n: r + n, 2, depends_on=up)
    _wait(tm, up, "done")
    t = _wait(tm, down, "done")
    assert t["result"] == 7


def test_chain_immediate_when_upstream_already_done(conn) -> None:
    tm = TaskManager(conn=conn)
    up = tm.submit(lambda: "A")
    _wait(tm, up, "done")
    down = tm.submit(lambda r: r + "B", depends_on=up)
    t = _wait(tm, down, "done")
    assert t["result"] == "AB"


def test_chain_fails_when_upstream_errors(conn) -> None:
    tm = TaskManager(conn=conn)

    def boom():
        raise RuntimeError("上游炸了")

    up = tm.submit(boom)
    down = tm.submit(lambda r: r, depends_on=up)
    _wait(tm, up, "error")
    t = _wait(tm, down, "error")
    assert tm.get(up)["id"] in t["message"]
    assert "error" in t["status"]


def test_chain_fails_when_upstream_missing(conn) -> None:
    tm = TaskManager(conn=conn)
    down = tm.submit(lambda r: r, depends_on="nonexistent")
    t = _wait(tm, down, "error")
    assert "不存在" in t["message"]


def test_chain_cancelled_downstream_never_runs(conn) -> None:
    tm = TaskManager(conn=conn, cpu_workers=1)
    started = []

    def slow():
        time.sleep(0.8)
        return 1

    up = tm.submit(slow)
    down = tm.submit(lambda r: started.append(r), depends_on=up)
    assert tm.cancel(down) is True
    _wait(tm, up, "done")
    time.sleep(0.3)
    t = tm.get(down)
    assert t["status"] == "cancelled"
    assert started == []


# ── 多上游依赖（depends_on 列表）────────────────────────────

def test_multi_upstream_results_in_order(conn) -> None:
    tm = TaskManager(conn=conn, cpu_workers=2)
    a = tm.submit(lambda: "A")
    b = tm.submit(lambda: "B")
    down = tm.submit(lambda rs, suf: "-".join(rs) + suf, "!", depends_on=[a, b])
    _wait(tm, a, "done"); _wait(tm, b, "done")
    t = _wait(tm, down, "done")
    assert t["result"] == "A-B!"


def test_multi_upstream_one_fails_downstream_errors(conn) -> None:
    tm = TaskManager(conn=conn)

    def boom():
        raise RuntimeError("第二个上游炸了")

    a = tm.submit(lambda: 1)
    b = tm.submit(boom)
    down = tm.submit(lambda rs: rs, depends_on=[a, b])
    _wait(tm, b, "error")
    t = _wait(tm, down, "error")
    assert tm.get(b)["id"] in t["message"]


def test_multi_upstream_partial_done_then_launch(conn) -> None:
    """a 先完成、b 后完成：a 的结果应被缓存，b 完成后按序注入。"""
    import threading as _th
    tm = TaskManager(conn=conn, cpu_workers=2)
    gate = _th.Event()

    def wait_b():
        gate.wait(3.0)
        return "B"

    a = tm.submit(lambda: "A")
    b = tm.submit(wait_b)
    _wait(tm, a, "done")
    down = tm.submit(lambda rs: rs, depends_on=[a, b])
    time.sleep(0.2)
    assert tm.get(down)["status"] == "pending"
    gate.set()
    t = _wait(tm, down, "done")
    assert t["result"] == ["A", "B"]


# ── 取消级联 ────────────────────────────────────────────────

def test_cancel_pending_upstream_cascades(conn) -> None:
    """pending 上游被取消 → 下游立即 error，不再永久 waiting。"""
    import threading as _th
    tm = TaskManager(conn=conn, cpu_workers=1)
    gate = _th.Event()
    blocker = tm.submit(gate.wait, 3.0)   # 占满唯一的 cpu worker
    up = tm.submit(lambda: 1)             # 因此 up 保持 pending（排队）
    down = tm.submit(lambda r: r, depends_on=up)
    assert tm.cancel(up) is True          # 取消 pending 上游 → 级联
    t = _wait(tm, down, "error")
    assert tm.get(up)["id"] in t["message"]
    gate.set()                            # 释放 blocker，避免遗留后台线程
    _wait(tm, blocker, "done")


def test_fanout_two_downstreams_both_run(conn) -> None:
    tm = TaskManager(conn=conn, cpu_workers=2)
    up = tm.submit(lambda: 10)
    d1 = tm.submit(lambda r: r + 1, depends_on=up)
    d2 = tm.submit(lambda r: r * 2, depends_on=up)
    _wait(tm, up, "done")
    assert _wait(tm, d1, "done")["result"] == 11
    assert _wait(tm, d2, "done")["result"] == 20


# ── pipeline 助手 ───────────────────────────────────────────

def test_pipeline_chains_steps(conn) -> None:
    tm = TaskManager(conn=conn)
    final = tm.pipeline([lambda: 1, lambda r: r + 1, lambda r: r * 10])
    t = _wait(tm, final, "done")
    assert t["result"] == 20
    # 链回溯：final.depends_on 应指向上一步 id
    mid = t["depends_on"]
    assert tm.get(mid)["result"] == 2


def test_pipeline_empty_raises(conn) -> None:
    tm = TaskManager(conn=conn)
    with pytest.raises(ValueError):
        tm.pipeline([])

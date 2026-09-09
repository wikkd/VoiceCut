"""TaskManager: two pools + cancellation semantics."""
from __future__ import annotations

import time

from app.tasks import TaskCancelled, TaskManager


def _wait_status(tm: TaskManager, tid: str, wanted: tuple[str, ...]) -> dict:
    for _ in range(100):
        t = tm.get(tid)
        if t and t["status"] in wanted:
            return t
        time.sleep(0.02)
    t = tm.get(tid)
    assert t is not None
    return t


def test_done_task() -> None:
    tm = TaskManager()
    tid = tm.submit(lambda: 42)
    t = _wait_status(tm, tid, ("done", "error"))
    assert t["status"] == "done" and t["result"] == 42


def test_task_cancelled_exception() -> None:
    tm = TaskManager()

    def worker():
        raise TaskCancelled()

    tid = tm.submit(worker)
    t = _wait_status(tm, tid, ("cancelled", "error"))
    assert t["status"] == "cancelled"


def test_cancel_pending() -> None:
    tm = TaskManager(cpu_workers=1)
    hold = tm.submit(lambda: time.sleep(0.6))  # occupies the single cpu worker
    tid = tm.submit(lambda: 1)                 # stays pending
    assert tm.cancel(tid) is True
    t = tm.get(tid)
    assert t["status"] == "cancelled"
    _wait_status(tm, hold, ("done", "error"))


def test_cancel_running_sets_flag() -> None:
    tm = TaskManager()

    def worker():
        while not tm.cancelled(tm.current_task_id()):
            time.sleep(0.01)

    tid = tm.submit(worker)
    time.sleep(0.1)
    assert tm.cancel(tid) is True
    # worker exits once it observes the cancelled flag
    t = _wait_status(tm, tid, ("done", "cancelled", "error"))
    assert t["status"] == "cancelled"  # worker observed the flag and aborted


def test_cleanup_prunes_finished_tasks() -> None:
    tm = TaskManager()
    tid = tm.submit(lambda: 42)
    _wait_status(tm, tid, ("done", "error"))
    assert tm.get(tid) is not None
    tm.cleanup(max_age=-1)  # 终态任务立即过期
    assert tm.get(tid) is None


def test_cleanup_keeps_running_tasks() -> None:
    tm = TaskManager()
    tid = tm.submit(lambda: time.sleep(0.5))
    time.sleep(0.05)
    tm.cleanup(max_age=-1)
    assert tm.get(tid) is not None  # 运行中不清理
    _wait_status(tm, tid, ("done", "error"))


def test_submit_lazily_prunes_old_tasks() -> None:
    tm = TaskManager()
    tid1 = tm.submit(lambda: 1)
    _wait_status(tm, tid1, ("done", "error"))
    tm.cleanup(max_age=-1)  # tid1 过期
    tm.submit(lambda: 2)    # submit 内惰性清理
    assert tm.get(tid1) is None


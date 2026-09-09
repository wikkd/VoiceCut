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
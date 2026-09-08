"""后台任务管理器：长操作（解码/降噪/分离/转写/下载）异步执行 + 进度轮询。"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

_local = threading.local()


class TaskManager:
    """后台任务执行与状态跟踪。

    状态机: pending → running → done | error | cancelled
    前端通过 GET /api/tasks/<id> 轮询进度；worker 内可用 current_task_id() 拿自身任务 id。
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vc-task")
        self._tasks: dict[str, dict] = {}
        self._lock = threading.RLock()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "status": "pending",
                "progress": 0.0,
                "message": "排队中",
                "result": None,
                "error": None,
                "started": None,
                "finished": None,
            }

        def _run() -> None:
            _local.task_id = task_id
            with self._lock:
                self._tasks[task_id]["status"] = "running"
                self._tasks[task_id]["started"] = time.time()
            try:
                result = fn(*args, **kwargs)
                with self._lock:
                    t = self._tasks[task_id]
                    t["status"] = "done"
                    t["progress"] = 1.0
                    t["message"] = "完成"
                    t["result"] = result
                    t["finished"] = time.time()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    t = self._tasks[task_id]
                    t["status"] = "error"
                    t["message"] = str(exc)
                    t["error"] = repr(exc)
                    t["finished"] = time.time()
            finally:
                _local.task_id = None

        self._pool.submit(_run)
        return task_id

    def current_task_id(self) -> str | None:
        """worker 线程内调用，返回自身 task_id（非 worker 线程返回 None）。"""
        return getattr(_local, "task_id", None)

    def update(self, task_id: str, *, progress: float | None = None, message: str | None = None) -> None:
        """任务内部上报进度（worker 线程调用）。"""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            if progress is not None:
                t["progress"] = max(0.0, min(1.0, progress))
            if message is not None:
                t["message"] = message

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def cleanup(self, max_age: float = 3600.0) -> int:
        """清理超龄已完成任务，返回清理数。"""
        now = time.time()
        dead = []
        with self._lock:
            for tid, t in self._tasks.items():
                if t["status"] in ("done", "error", "cancelled") and t.get("finished"):
                    if now - t["finished"] > max_age:
                        dead.append(tid)
            for tid in dead:
                self._tasks.pop(tid, None)
        return len(dead)

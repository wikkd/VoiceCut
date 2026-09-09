"""Background task manager: async long ops + progress polling + cancellation.

Two worker pools:
- gpu pool (1 worker): whisper / ECAPA / demucs -- GPU memory is shared, so
  these are serialized to avoid CUDA OOM.
- cpu pool (2 workers): ffmpeg / denoise / downloads / dataset export.

Status machine: pending -> running -> done | error | cancelled
Frontend polls GET /api/tasks/<id>; workers read their own id via
current_task_id() and report progress with update().
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.log import get_logger

_local = threading.local()


class TaskCancelled(RuntimeError):
    """Raised by workers at a safe checkpoint when the task was cancelled."""


class TaskManager:
    """Background task execution with status tracking and cancellation."""

    def __init__(self, gpu_workers: int = 1, cpu_workers: int = 2) -> None:
        self._gpu_pool = ThreadPoolExecutor(
            max_workers=gpu_workers, thread_name_prefix="vc-gpu")
        self._cpu_pool = ThreadPoolExecutor(
            max_workers=cpu_workers, thread_name_prefix="vc-cpu")
        self._tasks: dict[str, dict] = {}
        self._cancelled: dict[str, bool] = {}
        self._lock = threading.RLock()
        self._log = get_logger()

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        gpu: bool = False,
        **kwargs: Any,
    ) -> str:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "status": "pending",
                "progress": 0.0,
                "message": "queued",
                "result": None,
                "error": None,
                "started": None,
                "finished": None,
            }
            self._cancelled[task_id] = False
        pool = self._gpu_pool if gpu else self._cpu_pool
        pool.submit(self._run, task_id, fn, args, kwargs)
        return task_id

    def _run(self, task_id: str, fn: Callable, args: tuple, kwargs: dict) -> None:
        _local.task_id = task_id
        with self._lock:
            if self._cancelled.get(task_id):
                self._finish(task_id, "cancelled", "cancelled")
                _local.task_id = None
                return
            self._tasks[task_id]["status"] = "running"
            self._tasks[task_id]["started"] = time.time()
        self._log.info("task %s start", task_id)
        try:
            result = fn(*args, **kwargs)
            with self._lock:
                if self._cancelled.get(task_id):
                    self._finish(task_id, "cancelled", "cancelled")
                else:
                    self._finish(task_id, "done", "done", result=result)
            self._log.info("task %s done", task_id)
        except TaskCancelled:
            with self._lock:
                self._finish(task_id, "cancelled", "cancelled")
            self._log.info("task %s cancelled", task_id)
        except Exception as exc:  # noqa: BLE001
            self._log.exception("task %s error", task_id)
            with self._lock:
                t = self._tasks.get(task_id)
                if t is not None:
                    t["status"] = "error"
                    t["message"] = str(exc)
                    t["error"] = repr(exc)
                    t["finished"] = time.time()
        finally:
            with self._lock:
                self._cancelled.pop(task_id, None)
            _local.task_id = None

    def _finish(self, task_id: str, status: str, message: str,
                result: Any | None = None) -> None:
        t = self._tasks.get(task_id)
        if t is None:
            return
        t["status"] = status
        t["message"] = message
        t["result"] = result
        t["finished"] = time.time()
        if status == "done":
            t["progress"] = 1.0

    def current_task_id(self) -> str | None:
        """Return the current worker's task id (None outside a worker)."""
        return getattr(_local, "task_id", None)

    def update(self, task_id: str, *, progress: float | None = None,
               message: str | None = None) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            if progress is not None:
                t["progress"] = max(0.0, min(1.0, progress))
            if message is not None:
                t["message"] = message

    def cancelled(self, task_id: str) -> bool:
        with self._lock:
            return bool(self._cancelled.get(task_id))

    def cancel(self, task_id: str) -> bool:
        """Request cancellation. Returns True if the task can still be cancelled."""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None or t["status"] not in ("pending", "running"):
                return False
            self._cancelled[task_id] = True
            if t["status"] == "pending":
                t["status"] = "cancelled"
                t["message"] = "cancelled"
                t["finished"] = time.time()
            return True

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def cleanup(self, max_age: float = 3600.0) -> int:
        now = time.time()
        dead = []
        with self._lock:
            for tid, t in self._tasks.items():
                if t["status"] in ("done", "error", "cancelled") and t.get("finished"):
                    if now - t["finished"] > max_age:
                        dead.append(tid)
            for tid in dead:
                self._tasks.pop(tid, None)
                self._cancelled.pop(tid, None)
        return len(dead)
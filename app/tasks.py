"""Background task manager: async long ops + progress polling + cancellation.

Two worker pools:
- gpu pool (1 worker): whisper / ECAPA / demucs -- GPU memory is shared, so
  these are serialized to avoid CUDA OOM.
- cpu pool (2 workers): ffmpeg / denoise / downloads / dataset export.

Status machine: pending -> running -> done | error | cancelled
On restart, lingering pending/running rows are marked ``interrupted``.
Frontend polls GET /api/tasks/<id>; workers read their own id via
current_task_id() and report progress with update().

Persistence (optional): pass a sqlite connection (app.db, ``tasks`` table) and
every status transition is written through; ``get``/``all`` merge DB history so
tasks survive a server restart.

Task chains: ``submit(fn, *args, depends_on=upstream_id)`` registers the task
as pending until the upstream finishes; the worker is then started as
``fn(upstream_result, *args, **kwargs)``. If the upstream fails/cancels, the
dependent task is marked error immediately.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app import db as db_mod
from app.log import get_logger

_local = threading.local()

#: DB 历史保留时长（秒）：终态行超过该时长在 cleanup 时删除
DB_TTL = 7 * 86400.0


class TaskCancelled(RuntimeError):
    """Raised by workers at a safe checkpoint when the task was cancelled."""


class TaskManager:
    """Background task execution with status tracking and cancellation."""

    def __init__(self, conn: sqlite3.Connection | None = None,
                 gpu_workers: int = 1, cpu_workers: int = 2) -> None:
        self._gpu_pool = ThreadPoolExecutor(
            max_workers=gpu_workers, thread_name_prefix="vc-gpu")
        self._cpu_pool = ThreadPoolExecutor(
            max_workers=cpu_workers, thread_name_prefix="vc-cpu")
        self._tasks: dict[str, dict] = {}
        self._cancelled: dict[str, bool] = {}
        # depends_on -> [(task_id, fn, args, kwargs, gpu)]：等待上游完成的下游任务
        self._waiters: dict[str, list[tuple]] = {}
        self._conn = conn
        self._lock = threading.RLock()
        self._log = get_logger()
        if conn is not None:
            self._recover_interrupted()

    # ── persistence helpers ─────────────────────────────────
    def _dbw(self, sql: str, params: tuple) -> None:
        if self._conn is not None:
            db_mod.exec_write(self._conn, sql, params)

    def _persist_new(self, t: dict) -> None:
        self._dbw(
            "INSERT OR REPLACE INTO tasks"
            " (id, kind, status, progress, message, depends_on, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["kind"], t["status"], 0.0, t["message"],
             t.get("depends_on"), t.get("created") or time.time()))

    def _persist_state(self, t: dict) -> None:
        self._dbw(
            "UPDATE tasks SET status=?, progress=?, message=?, error=?,"
            " started=?, finished=? WHERE id=?",
            (t["status"], t["progress"], t["message"], t["error"],
             t["started"], t["finished"], t["id"]))

    def _persist_result(self, t: dict) -> None:
        result = t.get("result")
        try:
            raw = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            raw = repr(result)
        self._dbw(
            "UPDATE tasks SET status=?, progress=?, message=?, error=?,"
            " result=?, finished=? WHERE id=?",
            (t["status"], t["progress"], t["message"], t["error"],
             raw, t["finished"], t["id"]))

    def _recover_interrupted(self) -> None:
        """启动时把上次残留的 pending/running 行标记为 interrupted。"""
        self._dbw(
            "UPDATE tasks SET status='interrupted', message='服务重启，任务中断',"
            " finished=? WHERE status IN ('pending','running')",
            (time.time(),))

    # ── submission ──────────────────────────────────────────
    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        gpu: bool = False,
        kind: str = "",
        depends_on: str | None = None,
        **kwargs: Any,
    ) -> str:
        """提交后台任务，返回 task_id。

        ``depends_on``: 上游任务 id。上游 done 后以下游 worker 以
        ``fn(upstream_result, *args, **kwargs)`` 启动；上游 error/cancelled
        则下游直接标记 error，不会执行。
        """
        self.cleanup()  # 惰性清理历史终态任务，避免字典无限增长
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._tasks[task_id] = {
                "id": task_id,
                "kind": kind,
                "status": "pending",
                "progress": 0.0,
                "message": "queued",
                "result": None,
                "error": None,
                "started": None,
                "finished": None,
                "depends_on": depends_on,
                "created": time.time(),
            }
            self._cancelled[task_id] = False
        self._persist_new(self._tasks[task_id])

        if depends_on:
            up = self.get(depends_on)
            with self._lock:
                t = self._tasks[task_id]
                if up is None:
                    t["status"] = "error"
                    t["message"] = f"上游任务不存在: {depends_on}"
                    t["error"] = t["message"]
                    t["finished"] = time.time()
                elif up["status"] == "done":
                    pass  # 上游已完成 → 立即以结果启动
                elif up["status"] in ("error", "cancelled", "interrupted"):
                    t["status"] = "error"
                    t["message"] = f"上游任务 {depends_on} 状态为 {up['status']}，本任务不执行"
                    t["error"] = t["message"]
                    t["finished"] = time.time()
                else:
                    t["message"] = f"waiting: {depends_on}"
                    self._waiters.setdefault(depends_on, []).append(
                        (task_id, fn, args, kwargs, gpu))
            if up is not None and up["status"] == "done":
                pool = self._gpu_pool if gpu else self._cpu_pool
                pool.submit(self._run, task_id, fn, (up.get("result"),) + args, kwargs)
            self._persist_state(self._tasks[task_id])
            return task_id

        pool = self._gpu_pool if gpu else self._cpu_pool
        pool.submit(self._run, task_id, fn, args, kwargs)
        return task_id

    # ── worker lifecycle ────────────────────────────────────
    def _run(self, task_id: str, fn: Callable, args: tuple, kwargs: dict) -> None:
        _local.task_id = task_id
        with self._lock:
            if self._cancelled.get(task_id):
                self._finish(task_id, "cancelled", "cancelled")
                _local.task_id = None
                return
            self._tasks[task_id]["status"] = "running"
            self._tasks[task_id]["started"] = time.time()
        self._persist_state(self._tasks[task_id])
        self._log.info("task %s start", task_id)
        try:
            result = fn(*args, **kwargs)
            with self._lock:
                if self._cancelled.get(task_id):
                    self._finish(task_id, "cancelled", "cancelled")
                else:
                    self._finish(task_id, "done", "done", result=result)
                    self._resolve_waiters(task_id, "done", result)
            self._log.info("task %s done", task_id)
        except TaskCancelled:
            with self._lock:
                self._finish(task_id, "cancelled", "cancelled")
                self._resolve_waiters(task_id, "cancelled", None)
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
                    self._persist_state(t)
                    self._resolve_waiters(task_id, "error", None)
        finally:
            with self._lock:
                self._cancelled.pop(task_id, None)
            _local.task_id = None

    def _resolve_waiters(self, upstream_id: str, status: str,
                         result: Any) -> None:
        """上游终态后处理下游任务：done → 启动；error/cancelled → 连带失败。"""
        with self._lock:
            waiters = self._waiters.pop(upstream_id, [])
        for tid, fn, args, kwargs, gpu in waiters:
            with self._lock:
                t = self._tasks.get(tid)
            if t is None or t["status"] != "pending":
                continue  # 已被取消
            if status == "done":
                t["message"] = "queued"
            else:
                t["status"] = "error"
                t["message"] = f"上游任务 {upstream_id} 状态为 {status}，本任务不执行"
                t["error"] = t["message"]
                t["finished"] = time.time()
            self._persist_state(t)
            if status == "done":
                pool = self._gpu_pool if gpu else self._cpu_pool
                pool.submit(self._run, tid, fn, (result,) + args, kwargs)

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
        self._persist_result(t)

    # ── introspection / control ─────────────────────────────
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
            # 节流落库：progress 变化 >=0.02 或 message 变化才写
            if progress is None or abs(t["progress"] - t.get("_saved_progress", -1)) >= 0.02 \
                    or (message is not None and message != t.get("_saved_message")):
                self._persist_state(t)
                t["_saved_progress"] = t["progress"]
                t["_saved_message"] = t["message"]

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
                self._persist_state(t)
            return True

    def _public(self, t: dict) -> dict:
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is not None:
                return self._public(dict(t))
        if self._conn is None:
            return None
        rows = db_mod.exec_query(
            self._conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            return None
        r = rows[0]
        try:
            result = json.loads(r["result"]) if r["result"] else None
        except Exception:  # noqa: BLE001
            result = None
        return {
            "id": r["id"], "kind": r["kind"], "status": r["status"],
            "progress": r["progress"], "message": r["message"],
            "error": r["error"], "result": result,
            "depends_on": r["depends_on"], "created": r["created"],
            "started": r["started"], "finished": r["finished"],
        }

    def all(self) -> list[dict]:
        self.cleanup()
        merged: dict[str, dict] = {}
        if self._conn is not None:
            for r in db_mod.exec_query(
                    self._conn,
                    "SELECT * FROM tasks ORDER BY created DESC LIMIT 200"):
                try:
                    result = json.loads(r["result"]) if r["result"] else None
                except Exception:  # noqa: BLE001
                    result = None
                merged[r["id"]] = {
                    "id": r["id"], "kind": r["kind"], "status": r["status"],
                    "progress": r["progress"], "message": r["message"],
                    "error": r["error"], "result": result,
                    "depends_on": r["depends_on"], "created": r["created"],
                    "started": r["started"], "finished": r["finished"],
                }
        with self._lock:
            for t in self._tasks.values():
                merged[t["id"]] = self._public(dict(t))
        return sorted(merged.values(), key=lambda t: t.get("created") or 0)

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
        # DB 历史：终态行保留 DB_TTL（7 天），远长于内存的 1h
        if self._conn is not None:
            self._dbw(
                "DELETE FROM tasks WHERE status IN"
                " ('done','error','cancelled','interrupted')"
                " AND finished IS NOT NULL AND finished < ?",
                (now - DB_TTL,))
        return len(dead)

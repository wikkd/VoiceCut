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

Task chains / pipelines:
- ``submit(fn, *args, depends_on=up)``: single upstream; the worker is started
  as ``fn(upstream_result, *args, **kwargs)``.
- ``submit(fn, *args, depends_on=[a, b])``: multiple upstreams; started as
  ``fn([result_a, result_b], *args, **kwargs)`` once ALL upstreams are done.
- Any upstream error/cancelled (including a pending upstream being cancelled)
  marks the dependent error immediately.
- ``pipeline([f1, f2, f3], *args)`` chains steps sequentially and returns the
  final task id; each step receives the previous step's result first.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

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
        # upstream_id -> [(task_id, fn, args, kwargs, gpu)]：等待上游完成的下游任务
        self._waiters: dict[str, list[tuple]] = {}
        self._conn = conn
        self._lock = threading.RLock()
        self._log = get_logger()
        if conn is not None:
            self._recover_interrupted()

    # ── persistence helpers ─────────────────────────────────
    def _dbw(self, sql: str, params: tuple) -> None:
        """持久化尽力而为：DB 异常（如关闭中的连接）只记日志，不拖垮任务执行。"""
        if self._conn is None:
            return
        try:
            db_mod.exec_write(self._conn, sql, params)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("task db write failed: %s", exc)

    def _dep_raw(self, dep: Any) -> Any:
        """depends_on 落库：list → JSON；单个 id 保持原样。"""
        return json.dumps(dep, ensure_ascii=False) if isinstance(dep, list) else dep

    @staticmethod
    def _deps_of(value: Any) -> list[str]:
        """depends_on 归一化为 id 列表（str → [str]；list → 去重保序）。"""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        out: list[str] = []
        for v in value:
            s = str(v)
            if s not in out:
                out.append(s)
        return out

    def _persist_new(self, t: dict) -> None:
        self._dbw(
            "INSERT OR REPLACE INTO tasks"
            " (id, kind, status, progress, message, depends_on, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (t["id"], t["kind"], t["status"], 0.0, t["message"],
             self._dep_raw(t.get("depends_on")), t.get("created") or time.time()))

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
        depends_on: str | Iterable[str] | None = None,
        **kwargs: Any,
    ) -> str:
        """提交后台任务，返回 task_id。

        ``depends_on``: 上游任务 id 或 id 列表。
        - 单上游：上游 done 后下游以 ``fn(upstream_result, *args, **kwargs)`` 启动。
        - 多上游：**全部** done 后以 ``fn([result_a, result_b, ...], *args)`` 启动
          （结果按 depends_on 顺序排列）。
        - 任一上游 error/cancelled（含等待中的上游被取消）→ 下游直接标记 error。
        """
        self.cleanup()  # 惰性清理历史终态任务，避免字典无限增长
        task_id = uuid.uuid4().hex[:12]
        deps = self._deps_of(depends_on)
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
                "depends_on": deps[0] if len(deps) == 1 else (deps or None),
                "created": time.time(),
            }
            self._cancelled[task_id] = False
        self._persist_new(self._tasks[task_id])

        launch: tuple | None = None  # (fn, prepended_args, kwargs, gpu)
        if deps:
            ups = {d: self.get(d) for d in deps}
            missing = [d for d, u in ups.items() if u is None]
            bad = {d: u["status"] for d, u in ups.items()
                   if u and u["status"] in ("error", "cancelled", "interrupted")}
            with self._lock:
                t = self._tasks[task_id]
                if missing:
                    t["status"] = "error"
                    t["message"] = f"上游任务不存在: {', '.join(missing)}"
                    t["error"] = t["message"]
                    t["finished"] = time.time()
                elif bad:
                    d = next(iter(bad))
                    t["status"] = "error"
                    t["message"] = f"上游任务 {d} 状态为 {bad[d]}，本任务不执行"
                    t["error"] = t["message"]
                    t["finished"] = time.time()
                else:
                    done = {d: u["result"] for d, u in ups.items()
                            if u["status"] == "done"}
                    t["_deps"] = deps
                    t["_dep_results"] = done
                    if len(done) == len(deps):
                        t["message"] = "queued"
                        launch = (fn, (self._dep_arg(deps, done),) + args, kwargs, gpu)
                    else:
                        waiting = [d for d in deps if d not in done]
                        t["message"] = "waiting: " + ",".join(waiting)
                        for d in waiting:
                            self._waiters.setdefault(d, []).append(
                                (task_id, fn, args, kwargs, gpu))
                self._persist_state(t)
            if launch is not None:
                pool = self._gpu_pool if launch[3] else self._cpu_pool
                pool.submit(self._run, task_id, launch[0], launch[1], launch[2])
            return task_id

        pool = self._gpu_pool if gpu else self._cpu_pool
        pool.submit(self._run, task_id, fn, args, kwargs)
        return task_id

    @staticmethod
    def _dep_arg(deps: list[str], results: dict) -> Any:
        """下游首参：单上游 → 该结果；多上游 → 按 deps 顺序的结果列表。"""
        if len(deps) == 1:
            return results[deps[0]]
        return [results[d] for d in deps]

    def pipeline(self, steps: list[Callable], *args: Any,
                 gpu: bool = False, kind: str = "") -> str:
        """把步骤列表串成顺序任务链，返回最终任务 id。

        ``steps = [fn1, fn2, ...]``：fn1(*args) 先执行；后续每步以
        ``fn(上一步结果, *args)`` 接收前序结果。任何一步失败，后续步骤
        自动标记 error 不执行。中间各步可用 ``get(final)["depends_on"]``
        沿链回溯。
        """
        if not steps:
            raise ValueError("pipeline 需要至少一个步骤")
        prev = self.submit(steps[0], *args, gpu=gpu, kind=kind)
        for fn in steps[1:]:
            prev = self.submit(fn, depends_on=prev, gpu=gpu, kind=kind)
        return prev

    # ── worker lifecycle ────────────────────────────────────
    def _run(self, task_id: str, fn: Callable, args: tuple, kwargs: dict) -> None:
        _local.task_id = task_id
        with self._lock:
            if self._cancelled.get(task_id):
                self._finish(task_id, "cancelled", "cancelled")
                self._resolve_waiters(task_id, "cancelled", None)
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
                self._resolve_waiters(task_id,
                                      "cancelled" if self._cancelled.get(task_id)
                                      else "done", result)
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
        """上游终态后处理下游：done → 收齐结果启动；error/cancelled → 连带失败。"""
        with self._lock:
            waiters = self._waiters.pop(upstream_id, [])
        launches: list[tuple] = []
        for tid, fn, args, kwargs, gpu in waiters:
            with self._lock:
                t = self._tasks.get(tid)
                if t is None or t["status"] != "pending":
                    continue  # 已被取消
                if status != "done":
                    t["status"] = "error"
                    t["message"] = f"上游任务 {upstream_id} 状态为 {status}，本任务不执行"
                    t["error"] = t["message"]
                    t["finished"] = time.time()
                    self._persist_state(t)
                    continue
                deps = t.get("_deps") or [upstream_id]
                results = dict(t.get("_dep_results") or {})
                results[upstream_id] = result
                t["_dep_results"] = results
                if len(results) < len(deps):
                    continue  # 还在等其它上游
                t["message"] = "queued"
                self._persist_state(t)
            launches.append((tid, fn, (self._dep_arg(deps, results),) + args,
                             kwargs, gpu))
        for tid, fn, pargs, kwargs, gpu in launches:
            pool = self._gpu_pool if gpu else self._cpu_pool
            pool.submit(self._run, tid, fn, pargs, kwargs)

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
        """Request cancellation. Returns True if the task can still be cancelled.

        取消等待中（pending）的任务会级联：其下游任务立即标记 error，不会执行。
        """
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
                self._resolve_waiters(task_id, "cancelled", None)
            return True

    def _public(self, t: dict) -> dict:
        return {k: v for k, v in t.items() if not k.startswith("_")}

    def _db_row_to_task(self, r: dict) -> dict:
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

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is not None:
                return self._public(dict(t))
        if self._conn is None:
            return None
        rows = db_mod.exec_query(
            self._conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._db_row_to_task(rows[0]) if rows else None

    def all(self) -> list[dict]:
        self.cleanup()
        merged: dict[str, dict] = {}
        if self._conn is not None:
            for r in db_mod.exec_query(
                    self._conn,
                    "SELECT * FROM tasks ORDER BY created DESC LIMIT 200"):
                merged[r["id"]] = self._db_row_to_task(r)
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

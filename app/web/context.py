"""WebContext：Flask 路由/worker 共享的配置、存储、任务与助手函数。"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from app import dataset as dataset_mod
from app import db as db_mod
from app import gptsovits as gptsovits_mod
from app import project as project_mod
from app import subtitles as subtitles_mod
from app.audio_ops import compute_peaks, wav_duration
from app.config import AppConfig
from app.ffmpeg_util import media_duration
from app.media_store import MediaItem, MediaStore
from app.tasks import TaskManager

# 下载链接保留时长（秒）
DL_TTL = 3600.0
# /api/training/status 服务端缓存 TTL（秒）
TRAINING_STATUS_TTL = 4.0


class WebContext:
    """封装共享可变状态与助手，Blueprint 通过 current_app.extensions["vc_ctx"] 访问。"""

    def __init__(self, cfg: AppConfig, store: MediaStore,
                 tasks: TaskManager, log) -> None:
        self.cfg = cfg
        self.store = store
        self.tasks = tasks
        self.log = log
        self.downloadable: dict[str, dict] = {}      # key -> {"path", "ts"}
        self.dl_lock = threading.RLock()
        self.training_tasks: dict[str, str] = {}     # role_id -> task_id
        self.training_lock = threading.Lock()
        self.training_status_cache: dict = {"key": None, "ts": 0.0, "data": None}
        # 项目级自动分析去重：project_id -> 运行中/排队中的任务 id；
        # auto_analyze_pending 标记“当前分析完成后需再跑一轮”（批量导入收敛）。
        self.auto_analyze_tasks: dict[str, str] = {}
        self.auto_analyze_pending: dict[str, bool] = {}
        self.auto_analyze_lock = threading.Lock()

    # ── item JSON ─────────────────────────────────────────────
    def item_json(self, item: MediaItem) -> dict:
        video_url = f"/api/video/{item.id}" if item.preview_mp4 else None
        if item.extra.get("proxy_url"):
            video_url = item.extra["proxy_url"]
        return {
            "id": item.id, "name": item.name, "kind": item.kind,
            "project_id": item.project_id or "",
            "duration": item.duration, "sample_rate": item.sample_rate,
            "audio_url": f"/api/audio/{item.id}",
            "video_url": video_url,
            "peaks_url": f"/api/peaks/{item.id}",
            "subs_url": f"/api/subtitles/{item.id}",
            "derived_from": item.derived_from,
            "source": item.source,
            "extra": self.item_extra(item),
        }

    def item_extra(self, item: MediaItem) -> dict:
        """Item extra 去掉重量级 peaks（经 peaks_url 单独取）。"""
        extra = dict(item.extra)
        extra.pop("peaks", None)
        return extra

    def default_project_id(self) -> str:
        return db_mod.ensure_default_project(db_mod.get_conn(self.cfg.workdir))

    def register_item(self, *, wav: Path, name: str, kind: str,
                      item_id: str | None = None, source: str = "",
                      preview: Path | None = None, derived_from: str | None = None,
                      project_id: str | None = None,
                      extra: dict | None = None) -> MediaItem:
        item_id = item_id or self.store.new_id()
        project_id = project_id or self.default_project_id()
        duration = wav_duration(wav) or media_duration(wav)  # wav 直读头部，秒回
        peaks = compute_peaks(wav)
        item = MediaItem(
            id=item_id, name=name, wav_path=wav, duration=duration,
            sample_rate=48000, preview_mp4=preview, source=source,
            derived_from=derived_from, kind=kind, project_id=project_id,
            extra={**(extra or {}), "peaks": peaks},
        )
        self.store.add(item)
        return item

    def unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        i = 2
        while True:
            cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
            if not cand.exists():
                return cand
            i += 1

    def fmt_ts(self, t: float) -> str:
        t = max(0.0, float(t))
        m = int(t // 60)
        s = t - m * 60
        return f"{m:02d}-{s:04.1f}"

    # ── 下载注册（TTL 惰性清理） ──────────────────────────────
    def prune_downloadable(self) -> None:
        now = time.time()
        with self.dl_lock:
            for k in [k for k, v in self.downloadable.items()
                      if now - v["ts"] > DL_TTL]:
                self.downloadable.pop(k, None)

    def register_download(self, path: Path) -> str:
        key = uuid.uuid4().hex
        self.prune_downloadable()
        with self.dl_lock:
            self.downloadable[key] = {"path": path, "ts": time.time()}
        return key

    # ── 字幕 ────────────────────────────────────────────────
    def load_subs(self, item: MediaItem) -> list[dict]:
        f = item.extra.get("subs_file")
        if not f or not Path(f).exists():
            return []
        return [s.to_dict() for s in subtitles_mod.parse_subtitle_file(f)]

    # ── 说话人 / 训练共享助手 ────────────────────────────────
    def project_pool(self, project_id: str) -> list[dict]:
        if not project_id:
            project_id = self.default_project_id()
        return project_mod.load_pool(self.cfg.workdir, project_id)["characters"]

    def project_items(self, project_id: str) -> list[MediaItem]:
        if not project_id:
            project_id = self.default_project_id()
        return self.store.by_project(project_id)

    def role_clips(self, project_id: str, role_id: str):
        """项目内该角色的有效片段 + 源 wav 映射 + 多数语言。"""
        segs: list = []
        sources: dict = {}
        lang_counts: dict[str, int] = {}
        for item in self.project_items(project_id):
            if not item.wav_path or not Path(item.wav_path).exists():
                continue
            proj = project_mod.load_project(self.cfg.workdir, item.id)
            for s in proj["segments"]:
                if s.get("characterId") != role_id:
                    continue
                text = (s.get("text") or "").strip()
                if not text:
                    continue
                dur = float(s.get("end", 0)) - float(s.get("start", 0))
                if dur < 0.5:
                    continue
                lang = gptsovits_mod.lang_map(s.get("language") or "JP")
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
                segs.append(dataset_mod.DatasetSegment(
                    start=float(s.get("start", 0)), end=float(s.get("end", 0)),
                    text=text, item_id=item.id, language=lang,
                    speaker="", note=""))
                sources[item.id] = item.wav_path
        majority = max(lang_counts, key=lang_counts.get) if lang_counts else "ja"
        return segs, sources, majority

    def role_exp(self, settings: dict, role: dict, override: str = "") -> str:
        exp = (override or role.get("exp") or "").strip()
        if exp:
            return gptsovits_mod.sanitize(exp)
        base = gptsovits_mod.sanitize(role.get("name") or "speaker")
        return base if gptsovits_mod.exp_dir(settings, base).exists() \
            else gptsovits_mod.unique_exp(settings, base)

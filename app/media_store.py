"""素材注册表：内存中的媒体条目管理。

每个条目对应一条“可剪辑的音频”及其可选视频预览。
降噪/分离/转写产物都会成为新条目，保留溯源关系。
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MediaItem:
    """一条可剪辑素材。

    - wav_path    : 内部工作 WAV (48kHz 单声道 PCM)，波形/播放/处理都以它为准
    - preview_mp4 : 可选，浏览器视频预览文件（B站/本地视频导入时生成）
    - source      : 原始来源路径或 URL
    - derived_from: 父条目 id（降噪/分离等产物）
    """

    id: str
    name: str
    wav_path: Path
    duration: float
    sample_rate: int
    source: str = ""
    preview_mp4: Path | None = None
    derived_from: str | None = None
    kind: str = "audio"          # audio | video | vocal | instrumental | denoised | bilibili
    created: float = field(default_factory=lambda: __import__("time").time())
    extra: dict = field(default_factory=dict)


class MediaStore:
    """线程安全的内存素材注册表。"""

    def __init__(self) -> None:
        self._items: dict[str, MediaItem] = {}
        self._lock = __import__("threading").RLock()
        self._counter = itertools.count(1)

    def add(self, item: MediaItem) -> MediaItem:
        with self._lock:
            self._items[item.id] = item
        return item

    def get(self, item_id: str) -> MediaItem | None:
        with self._lock:
            return self._items.get(item_id)

    def require(self, item_id: str) -> MediaItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(f"素材不存在: {item_id}")
        return item

    def all(self) -> list[MediaItem]:
        with self._lock:
            return list(self._items.values())

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items.pop(item_id, None)

    def new_id(self) -> str:
        with self._lock:
            n = next(self._counter)
        return f"m{n:04d}"

    def unique_name(self, base: str) -> str:
        """生成不重名的素材名（追加序号）。"""
        with self._lock:
            names = {i.name for i in self._items.values()}
        if base not in names:
            return base
        for n in itertools.count(1):
            cand = f"{base}_{n}"
            if cand not in names:
                return cand

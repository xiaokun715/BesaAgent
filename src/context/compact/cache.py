"""压缩结果缓存（`FR-C-04` / `NFR-C-03`）。

**为什么必须有**：没有它，**每多一轮就重压一次**。成本随轮数线性涨，
而且**没有任何一行代码看起来是错的** —— 这正是"看起来很安全"的那种失效。

**为什么是进程内而不是落库**：压缩是**优化**。缓存丢了只是慢一点，
而把它落库会引入一个新的不一致：库里的摘要可能是用**旧版本提示词**压的，
而代码已经换了 prompt —— 复用它得到的是"用旧标准压的"结果，没有任何症状。
所以缓存跟着进程走，进程重启即失效，与 prompt 版本天然对齐。

缓存键**必须含 ``prompt_version``**：否则改了提示词还会命中旧结果。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from context.types import CompressionLevel, ContextBlock

__all__ = ["CompactCache"]


class CompactCache:
    """一个带上限的 LRU 缓存。**键是内容指纹 + 级别 + 提示词版本。**"""

    __slots__ = ("_enabled", "_max_entries", "_items", "_hits", "_misses")

    def __init__(self, *, enabled: bool = True, max_entries: int = 256) -> None:
        self._enabled = enabled
        self._max_entries = max(1, max_entries)
        self._items: OrderedDict[str, ContextBlock] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def __len__(self) -> int:
        return len(self._items)

    @staticmethod
    def key(fingerprint: str, level: CompressionLevel, prompt_version: str) -> str:
        """缓存键。三个部分缺一不可：
        内容变了要重压、级别不同产物不同、提示词改了不能用旧结果。"""
        raw = f"{prompt_version}\x1f{level}\x1f{fingerprint}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, key: str) -> ContextBlock | None:
        if not self._enabled:
            return None
        block = self._items.get(key)
        if block is None:
            self._misses += 1
            return None
        self._items.move_to_end(key)   # LRU
        self._hits += 1
        return block

    def put(self, key: str, block: ContextBlock) -> None:
        if not self._enabled:
            return
        self._items[key] = block
        self._items.move_to_end(key)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
        self._hits = 0
        self._misses = 0

"""压缩账本（`FR-C-05`）。

**这是"当时到底有什么"唯一的事后依据。** 摘要本身回答不了这个问题 ——
它就是被丢掉的细节的替代品，问它等于问一个记不清的人。

账本同时是**调参的唯一反馈来源**：`trigger_ratio` 定得对不对、L2 到底省了多少、
哪些段老是被砍 —— 全部只能从这里读出来。
"""

from __future__ import annotations

from collections.abc import Sequence

from foundation.clock import Clock, SystemClock

from context.types import CompressionLevel, LedgerEntry, Segment

__all__ = ["Ledger"]


class Ledger:
    """一次请求内累积的压缩账。"""

    __slots__ = ("_entries", "_clock")

    def __init__(self, clock: Clock | None = None) -> None:
        self._entries: list[LedgerEntry] = []
        self._clock = clock or SystemClock()

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def record(
        self,
        *,
        level: CompressionLevel,
        segment: Segment,
        message_ids: Sequence[str] = (),
        before_tokens: int = 0,
        after_tokens: int = 0,
        reason: str = "",
        cache_key: str = "",
    ) -> LedgerEntry:
        entry = LedgerEntry(
            level=level,
            segment=segment,
            message_ids=tuple(message_ids),
            before_tokens=int(before_tokens),
            after_tokens=int(after_tokens),
            reason=reason,
            cache_key=cache_key,
            at=self._clock.now(),
        )
        self._entries.append(entry)
        return entry

    def saved_tokens(self) -> int:
        return sum(entry.saved for entry in self._entries)

    def describe(self) -> str:
        """一行摘要，给日志/事件用。"""
        if not self._entries:
            return "未发生压缩"
        parts = [
            f"{e.level}({e.segment}) {e.before_tokens}→{e.after_tokens}"
            for e in self._entries
        ]
        return f"{len(self._entries)} 次压缩，共省 {self.saved_tokens()} token：" + "；".join(parts)

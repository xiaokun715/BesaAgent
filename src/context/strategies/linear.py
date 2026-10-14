"""策略一：**线性上下文 + 多级压缩**（`FR-C-02`）。

一条完整的历史，越早压得越狠。压缩的具体做法全部交给 :mod:`context.compact` ——
本文件只负责「给它一个目标，把结果放到 ``window`` 这一段里」。

**尾部不动**由压缩器保证（``keep_recent_turns``）：最近几轮是模型的"工作记忆"。
"""

from __future__ import annotations

import logging

from context.compact.levels import Compactor
from context.context import ContextRequest
from context.resolver import MemoryResolver
from context.strategy import Collected
from context.types import ContextBlock, StrategyName

__all__ = ["LinearStrategy"]

_log = logging.getLogger(__name__)


class LinearStrategy:
    """线性 + 多级压缩。"""

    name: StrategyName = "linear"

    __slots__ = ("_compactor", "_resolver", "_keep_recent_turns")

    def __init__(
        self,
        *,
        compactor: Compactor,
        resolver: MemoryResolver | None = None,
        keep_recent_turns: int | None = None,
    ) -> None:
        self._compactor = compactor
        self._resolver = resolver
        self._keep_recent_turns = keep_recent_turns

    @property
    def compactor(self) -> Compactor:
        return self._compactor

    async def collect(
        self, request: ContextRequest, *, target_window_tokens: int
    ) -> Collected:
        notes: list[str] = []
        degraded = ""

        result = await self._compactor.compact(
            request.history,
            target_tokens=target_window_tokens,
            keep_recent=self._keep_recent_turns,
            segment="window",
            reason="线性策略：预算驱动",
        )
        if result.note:
            notes.append(result.note)
        if result.insufficient:
            # **如实上报**：到顶仍不够时不静默截断（`FR-C-13`）。
            # 让预算层去按优先级砍，而不是在这里偷着丢。
            degraded = result.note or "压缩到最高级仍超出预算"

        blocks: dict[str, tuple[ContextBlock, ...]] = {"window": result.blocks}

        # 长期记忆（`FR-C-10`）：线性策略也带它 —— 需求强制的是策略二，
        # 但"跨会话记得住"这件事对任何策略都成立，没有理由只在一条路上做。
        if self._resolver is not None and self._resolver.available:
            recalled = await self._resolver.recall(request)
            if recalled:
                blocks["long_term"] = recalled
        return Collected(
            blocks=blocks, ledger=result.entries, degraded=degraded, notes=tuple(notes)
        )

"""上下文管理策略的**契约与选择**（`FR-C-01`）。

**多种管理方式并存**，而调用方不该知道现在跑的是哪一条（`MC-14`）。
所以策略是**可替换的实现**，而 `ContextManager` 是对外唯一的面。

两条内置策略（都在 :mod:`context.strategies` 里）：

| 策略 | 组织方式 | 回答 |
|---|---|---|
| `linear` | 一条历史，**越早压得越狠** | 单任务长链路 |
| `tiered` | 按**时间尺度分层**，各层各机制 | 跨天、跨会话 |

它们**共享同一批零件**（`compact/` 的压缩、预算、选择器），差别只在组装方式 ——
这正是 `compact/` 独立成文件夹的理由（`MC-15`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from context.context import ContextRequest
from context.types import ContextBlock, LedgerEntry, Segment, StrategyName

__all__ = ["Collected", "ContextStrategy", "resolve_strategy"]


@dataclass(frozen=True)
class Collected:
    """策略采集到的**候选块**（按段分组，段内已按重要性排序）。

    这一步**还不受预算约束** —— 预算是下一步的事。分两步是为了让
    「先收集、再看装不下什么」这条顺序成立：反过来的话，策略就得自己
    知道预算怎么算，而那会把预算逻辑复制到每条策略里。
    """

    blocks: Mapping[Segment, tuple[ContextBlock, ...]] = field(default_factory=dict)
    #: 这次采集过程中发生的压缩账（`FR-C-05`）。**压缩器每次调用一个账本**，
    #: 由策略把它带上来 —— 否则"这次请求压了几次"会读成"至今一共压了几次"。
    ledger: tuple[LedgerEntry, ...] = ()
    #: 被去重掉的（内容摘要），给排障用（`NFR-C-08`）
    deduped: tuple[str, ...] = ()
    #: 发生降级时的原因；空表示正常
    degraded: str = ""
    notes: tuple[str, ...] = ()


@runtime_checkable
class ContextStrategy(Protocol):
    """一条上下文管理方式。**实现只需要回答「候选是什么」。**"""

    name: StrategyName

    async def collect(
        self, request: ContextRequest, *, target_window_tokens: int
    ) -> Collected:
        """采集候选块。

        Args:
            target_window_tokens: 预算层给「历史那一段」的配额。
                策略据此决定压到多小 —— **不是自己猜一个数**。
        """
        ...


def resolve_strategy(
    name: StrategyName, available: Mapping[StrategyName, ContextStrategy]
) -> ContextStrategy:
    """按名字取策略。

    Raises:
        ValueError: 未知策略名。**错误信息里列出全部可用策略** ——
            与 ``FR-G-02`` 的逻辑名寻址同一条纪律：拼错是最常见的错误，
            只说「未知」会让人去翻文档。
    """
    strategy = available.get(name)
    if strategy is None:
        known = ", ".join(sorted(available)) or "（无）"
        raise ValueError(f"未知的上下文策略 {name!r}；已知：{known}")
    return strategy

"""上下文的**输入**（:class:`ContextRequest`）与**产出物**（:class:`BuiltContext`）。

产出物里除了消息，还带着**这一次是怎么被裁出来的** —— 预算画像、压缩账本、
被去重掉的东西。它们不是为了好看：`NFR-C-06` / `NFR-C-08` 要求
「这轮压缩了几次、砍了哪些段、丢了什么」**可查**，否则「模型为什么忘了」永远答不出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from context.types import (
    BudgetPlan,
    ContextBlock,
    ContextMessage,
    LedgerEntry,
    StrategyName,
)

__all__ = ["BuiltContext", "ContextRequest"]


@dataclass(frozen=True)
class ContextRequest:
    """一次「给我一个能塞进模型窗口的上下文」的请求。

    ``history`` **不含 system、也不含当前输入** —— 前两者由本模块自己安排位置，
    否则调用方一不留神就会给出两个 system，或者把当前输入混进历史里被压缩掉。
    """

    session_id: str
    #: 会话历史（领域形态，来自 runtime / repo）。顺序即对话顺序。
    history: Sequence[ContextMessage] = ()
    #: 当前这一轮的用户输入。**不可动**（`MC-12`）—— 它承载任务本身。
    current_input: str = ""
    #: system 提示。缺省由本模块给一条最小提示（而不是空 —— 空 system 会让
    #: "本模块负责上下文"这件事在某些厂商上变成"没有指令"）。
    system: str = ""
    #: 任务目标。**记忆召回用它 + 当前输入**，而不是只用当前输入 ——
    #: 「这个任务要做什么」比「这句话说了什么」更能命中相关的长期记忆。
    goal: str = ""
    caller: str | None = None
    trace_id: str = ""
    #: 单次覆盖配置里的策略（`FR-C-01`）。``None`` = 用配置。
    strategy: StrategyName | None = None

    def recall_query(self) -> str:
        """构造长期记忆的召回查询。"""
        parts = [part for part in (self.goal, self.current_input) if part.strip()]
        return "\n".join(parts)


@dataclass(frozen=True)
class BuiltContext:
    """产出物。``messages`` 是最终交给 ``gateway.chat`` 的那一串。"""

    messages: tuple[Any, ...] = ()
    budget: BudgetPlan | None = None
    ledger: tuple[LedgerEntry, ...] = ()
    #: 被去重掉的（内容摘要），给排障用 —— 「为什么这条记忆没进来」要有答案
    deduped: tuple[str, ...] = ()
    strategy: StrategyName = "linear"
    #: 发生了降级时的原因（压缩失败、记忆不可用、预算不足……）。
    #: **为空表示一切正常**；非空必须能在日志与事件里看到。
    degraded_reason: str = ""
    #: 产出的块（在生成 ``messages`` 之前的中间形态），供测试与排障
    blocks: tuple[ContextBlock, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.messages)

    @property
    def insufficient(self) -> bool:
        return bool(self.budget and self.budget.insufficient)

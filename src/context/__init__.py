"""上下文层：把「这一次给模型看什么」变成一件**可计算、可追溯**的事。

**它解决的问题**：七个测试阶段是长链路，历史会长过窗口、贵过预算。
而"怎么把历史交给模型"这件事**每次都发生，错了却不会报错** ——
悄悄截断和认真压缩，从外面看是一样的。

**两条纪律**（读代码前先记住）：

1. **压缩不可逆，所以丢了什么必须可查**。每一次压缩都记一笔账
   （:mod:`context.compact.ledger`），摘要带着**能被取回原文的范围标识**
   （:func:`context.manager.ContextManager.rehydrate`）。
2. **只压中段**。system 与当前输入不可动（任务本身），最近若干轮不动（工作记忆），
   压的永远是中间那段更早的历史。

**两种管理方式并存**（`FR-C-01`），共享同一批零件：

- :mod:`context.strategies` —— 策略（线性+多级压缩 / 三层）；
- :mod:`context.compact` —— **多级压缩**（独立文件夹，两条策略共用）。

**依赖方向（硬约束）**：本模块依赖 ``gateway`` / ``memory`` / ``repo`` / ``foundation``。
**不得** import ``provider``（`DC-3` 已把它补进 import-linter 契约）、
``agent`` / ``multiagent``。

**对外只有一个面**：:class:`context.manager.ContextManager`。
上层不该知道现在跑的是哪条策略。
"""

from __future__ import annotations

from context.budget import BudgetPolicy, estimate_tokens
from context.builder import build_messages, from_message, rebuild
from context.compact import (
    CompactCache,
    CompactPolicy,
    CompactResult,
    CompactUnit,
    Compactor,
    Ledger,
    Summarizer,
    denoise,
)
from context.compressor import build_compactor
from context.context import BuiltContext, ContextRequest
from context.manager import (
    DEFAULT_EVENTS,
    ContextManager,
    ContextPolicy,
    EventSink,
    NullEventSink,
)
from context.resolver import MemoryResolver
from context.selector import ASSEMBLY_ORDER, dedupe, fit
from context.strategies import (
    InMemorySessionStore,
    LinearStrategy,
    SessionMemory,
    SessionMemoryStore,
    TieredPolicy,
    TieredStrategy,
    build_strategies,
)
from context.strategy import Collected, ContextStrategy, resolve_strategy
from context.types import (
    COMPRESSION_LEVELS,
    DROP_ORDER,
    SEGMENTS,
    STRATEGY_NAMES,
    BudgetPlan,
    ChatModel,
    CompressionLevel,
    ContextBlock,
    ContextMessage,
    LedgerEntry,
    Provenance,
    Role,
    Segment,
    StrategyName,
    ToolCallRef,
)

__all__ = [
    "ASSEMBLY_ORDER",
    "COMPRESSION_LEVELS",
    "DEFAULT_EVENTS",
    "DROP_ORDER",
    "SEGMENTS",
    "STRATEGY_NAMES",
    "BudgetPlan",
    "BudgetPolicy",
    "BuiltContext",
    "ChatModel",
    "Collected",
    "CompactCache",
    "CompactPolicy",
    "CompactResult",
    "CompactUnit",
    "Compactor",
    "CompressionLevel",
    "ContextBlock",
    "ContextManager",
    "ContextMessage",
    "ContextPolicy",
    "ContextRequest",
    "ContextStrategy",
    "EventSink",
    "InMemorySessionStore",
    "Ledger",
    "LedgerEntry",
    "LinearStrategy",
    "MemoryResolver",
    "NullEventSink",
    "Provenance",
    "Role",
    "Segment",
    "SessionMemory",
    "SessionMemoryStore",
    "StrategyName",
    "Summarizer",
    "TieredPolicy",
    "TieredStrategy",
    "ToolCallRef",
    "build_compactor",
    "build_messages",
    "build_strategies",
    "dedupe",
    "denoise",
    "estimate_tokens",
    "fit",
    "from_message",
    "rebuild",
    "resolve_strategy",
]

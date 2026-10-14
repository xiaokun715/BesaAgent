"""**上下文管理策略** —— 独立文件夹（与 ``compact/`` 并列）。

两种管理方式，**共享同一批零件**（压缩、预算、选择器、记忆接缝），
差别只在**组装方式**：

| 策略 | 组织方式 | 回答 |
|---|---|---|
| :class:`~context.strategies.linear.LinearStrategy` | 一条历史，**越早压得越狠** | 单任务长链路（一次跑几百轮） |
| :class:`~context.strategies.tiered.TieredStrategy` | 按**时间尺度分层**，各层各机制 | 跨天、跨会话的长期使用 |

把策略单独放一个包，是为了让「**加第三条策略**」这件事的代价是**加一个文件**，
而不是在某条策略的分支里再塞一个 ``if``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from context.compact.levels import Compactor
from context.resolver import MemoryResolver
from context.strategies.linear import LinearStrategy
from context.strategies.tiered import (
    InMemorySessionStore,
    SessionMemory,
    SessionMemoryStore,
    TieredPolicy,
    TieredStrategy,
)
from context.strategy import ContextStrategy
from context.types import STRATEGY_NAMES, StrategyName

__all__ = [
    "InMemorySessionStore",
    "LinearStrategy",
    "SessionMemory",
    "SessionMemoryStore",
    "TieredPolicy",
    "TieredStrategy",
    "build_strategies",
]


def build_strategies(
    *,
    compactor: Compactor,
    resolver: MemoryResolver | None = None,
    tiered_cfg: Mapping[str, Any] | None = None,
    linear_keep_recent_turns: int | None = None,
    session_store: SessionMemoryStore | None = None,
) -> dict[StrategyName, ContextStrategy]:
    """按配置建出**全部**策略。

    一次建全部（而不是只建被选中的那条）是刻意的：``strategy`` 可以**按调用覆盖**
    （`FR-C-01`），若只在启动时建一条，覆盖就只能对那一条生效 ——
    而"配置里选了 linear，某次调用想用 tiered"是完全正当的用法。
    """
    return {
        "linear": LinearStrategy(
            compactor=compactor,
            resolver=resolver,
            keep_recent_turns=linear_keep_recent_turns,
        ),
        "tiered": TieredStrategy(
            compactor=compactor,
            policy=TieredPolicy.from_config(tiered_cfg),
            resolver=resolver,
            store=session_store or InMemorySessionStore(),
        ),
    }

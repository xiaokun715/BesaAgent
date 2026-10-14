"""压缩的**门面** —— 对外只暴露一个入口，细节在 :mod:`context.compact`。

**为什么还要这么一层**：``src/context/`` 预置的桩文件里就有 ``compressor.py``
（另外七个是 ``budget / builder / context / manager / resolver / selector / types``）。
名字是契约的一部分，换名会让"谁在哪"重新变模糊。

于是它做两件事：

1. **把 ``compact/`` 组装起来**（摘要器 + 缓存 + 账本 + 策略），
   让上层不必知道压缩内部有四个零件；
2. **给两条策略一个共同的门面** —— 它们看到的是**同一个** ``Compactor``，
   这正是"多级压缩是共用零件"在代码上的形状（`MC-15`）。

**摘要器不可用时它照样能工作**：L1 是零成本的，没有 LLM 也能压掉一部分。
真的压不动时它**如实报告**（``insufficient``），不静默截断。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from foundation.clock import Clock

from context.compact.cache import CompactCache
from context.compact.levels import Compactor
from context.compact.policy import CompactPolicy
from context.compact.summarizer import Summarizer
from context.types import ChatModel

__all__ = ["build_compactor"]


def build_compactor(
    *,
    chat: ChatModel | None,
    cfg: Mapping[str, Any] | None = None,
    clock: Clock | None = None,
) -> Compactor:
    """按 ``context.linear.compact`` 段建压缩器。

    ``chat`` 为 ``None`` 时摘要器不可用 —— 压缩退化成**只有 L1**，
    这仍然是有用的（去噪 + 截断），但会在 ``CompactResult.note`` 里说清楚。
    """
    policy = CompactPolicy.from_config(cfg)
    summarizer = Summarizer(
        chat,
        alias=policy.summary_alias,
        max_tokens=policy.max_summary_tokens,
        prompt_version=policy.prompt_version,
    )
    return Compactor(
        summarizer=summarizer,
        policy=policy,
        cache=CompactCache(enabled=policy.cache),
        clock=clock,
    )

"""降级决策：主候选失败后，要不要换下一个。

**这里只有决策，没有动作** —— 换候选的循环在 ``gateway.py`` 的编排里。
分开的理由是：决策规则需要被**单独测试**（尤其是流式边界那条），
而循环混进去之后，「为什么这次没降级」就藏在一个大 while 里了。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gateway.retry import CallBudget

__all__ = ["FallbackDecision", "FallbackPolicy", "decide"]


@dataclass(frozen=True)
class FallbackPolicy:
    """降级配置。"""

    enabled: bool = True
    #: 流式已开始输出时是否仍允许降级。**默认 False，且不应轻易改**（见 :func:`decide`）。
    on_stream_started: bool = False

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> FallbackPolicy:
        data = dict(cfg or {})
        return cls(
            enabled=bool(data.get("enabled", True)),
            on_stream_started=bool(data.get("on_stream_started", False)),
        )


@dataclass(frozen=True)
class FallbackDecision:
    """要不要换下一个候选，以及**为什么**。

    ``reason`` 是给日志和排障用的：只记录「没降级」而不记原因，
    会让「为什么这次直接失败了」变成一个只能靠猜的问题。
    """

    proceed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.proceed


def decide(
    error: BaseException,
    *,
    policy: FallbackPolicy,
    budget: CallBudget,
    remaining_candidates: int,
    stream_committed: bool = False,
) -> FallbackDecision:
    """决定是否切换到下一个候选。

    判定顺序是**刻意的**，前面的规则优先于后面的：

    1. **流式已输出正文** → 禁止（``FR-G-05``）。
       正文已经吐给用户了，重新发起只会得到第二份不连贯的输出 ——
       这不是「容错」，是制造更糟的结果；而且用户会看到两段拼接的答案。
    2. **降级被关闭** → 禁止。
    3. **没有下一个候选** → 禁止（``no_more_candidates``）。
    4. **预算耗尽** → 禁止。``reason`` 透传 ``attempts`` / ``deadline``。
    5. 其余 → 允许。

    **第 3 条必须排在第 4 条前面**，这一点容易写反，而且写反了不会报错、
    只会让排障方向跑偏：默认配置下（``total_max_attempts=4``、每候选 2 次、2 个候选），
    「所有候选都失败」时预算**恰好同时**耗尽，于是正确语义的
    「全部候选失败」会被误报成「预算耗尽」。
    两者的修法完全相反 —— 前者该去看每个模型为什么挂，后者该去调预算。
    「还有候选没试过却停了」才是真正的预算耗尽。

    **跨能力降级不作为一条规则，因为它已被结构排除**：
    ``router.select()`` 在选链时就把不满足能力的候选全部过滤掉了，
    所以尝试链里的每个候选都必然满足本次能力要求 ——
    「把结构化输出的请求降级到不支持 JSON 的模型」这条错误在结构上不可能发生。
    把过滤集中在一处，比在每个决策点各判一次可靠。

    Args:
        error: 当前候选的失败。
        policy: 降级配置。
        budget: 总预算。**调用方不得在此之外自行判断次数**。
        remaining_candidates: 尝试链中**尚未尝试**的候选数。
        stream_committed: 是否已经输出过正文。
    """
    if stream_committed:
        return FallbackDecision(False, "stream_committed")

    if not policy.enabled:
        return FallbackDecision(False, "fallback_disabled")

    # 顺序见 docstring 第 3/4 条：先判「还有没有候选」，再判「预算够不够」
    if remaining_candidates <= 0:
        return FallbackDecision(False, "no_more_candidates")

    exhausted = budget.exhausted_reason()
    if exhausted is not None:
        # 这里不看 error.retryable：预算耗尽是**独立于错误类型**的终止条件
        return FallbackDecision(False, exhausted)

    return FallbackDecision(True, "proceed")

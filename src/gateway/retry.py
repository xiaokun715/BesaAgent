"""重试策略，以及 **``CallBudget``** —— 本模块最重要的东西。

``NFR-G-04``（故障不放大）要求「任何失败路径下上游请求总数有上界」，
``FR-G-12`` 要求「总 deadline 贯穿」。**这两条必须由同一个对象强制**，
否则会出现组合漏洞：

- 「重试次数没超，但时间超了」；
- 或者更糟：「时间没超，但已经打了 27 次上游」——
  ``重试 3 次 × 候选 3 个 × 降级链 3 层``，一次故障被放大成一场重试风暴。

所以本模块的核心不是「退避怎么算」（那是十几行的常识），
而是 **:class:`CallBudget` 这个唯一的配额发出点**。
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from foundation.clock import Clock, SystemClock

__all__ = ["CallBudget", "RetryPolicy", "compute_backoff", "should_retry"]

_log = logging.getLogger(__name__)


class CallBudget:
    """一次 gateway 调用的总预算。

    **``retry`` 与 ``fallback`` 都必须向它申请配额，且禁止自己数次数。**
    计数点只有一处，上界才是可知的 —— 这不是「约定」，是结构：
    ``try_acquire()`` 是唯一的 API，没有第二个地方能做这个决定。

    **deadline 用绝对时刻而非剩余秒数**：剩余秒数在多层传递中每次都要重算，
    且极易被误当成「本层的超时」而**重新起算** —— 那样总超时就成了
    「每一跳各 120 秒」，而不是「整次调用 120 秒」。
    """

    __slots__ = ("_max_attempts", "_clock", "_deadline", "_used")

    def __init__(
        self,
        *,
        max_attempts: int,
        clock: Clock | None = None,
        deadline_s: float | None = None,
    ) -> None:
        self._max_attempts = max(1, int(max_attempts))
        self._clock: Clock = clock or SystemClock()
        self._used = 0
        self._deadline: float | None = (
            None if deadline_s is None else self._clock.monotonic() + float(deadline_s)
        )

    # ---------------------------------------------------------------- 读
    @property
    def attempts_used(self) -> int:
        return self._used

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def remaining_attempts(self) -> int:
        return max(0, self._max_attempts - self._used)

    def remaining_s(self) -> float:
        """剩余时间；未设 deadline 时为无穷大。"""
        if self._deadline is None:
            return math.inf
        return max(0.0, self._deadline - self._clock.monotonic())

    def exhausted_reason(self) -> str | None:
        """预算耗尽的原因；未耗尽返回 ``None``。

        区分 ``attempts`` 与 ``deadline`` 是为了让错误信息能给出**正确的排障方向**：
        前者该调大 ``total_max_attempts``，后者该调大 ``deadline``。
        """
        if self._used >= self._max_attempts:
            return "attempts"
        if self.remaining_s() <= 0:
            return "deadline"
        return None

    # ---------------------------------------------------------------- 写
    def try_acquire(self) -> bool:
        """申请一次上游调用配额。**这是唯一的计数点。**

        Returns:
            ``True`` 表示可以发起；``False`` 表示预算已耗尽，调用方**必须停止**。
        """
        if self.exhausted_reason() is not None:
            return False
        self._used += 1
        return True

    def has_room_for(self, estimated_s: float) -> bool:
        """剩余预算是否够一次预计耗时 ``estimated_s`` 的尝试。

        用于避免「等一个必然超时的队」—— 例如限流排队预计 30 秒而只剩 2 秒，
        排队是纯粹的浪费，此时应当直接换候选（FR-G-06）。
        """
        return self.remaining_s() > estimated_s

    def __repr__(self) -> str:
        return (
            f"CallBudget(used={self._used}/{self._max_attempts}, "
            f"remaining_s={self.remaining_s():.3f}, "
            f"exhausted={self.exhausted_reason()!r})"
        )


@dataclass(frozen=True)
class RetryPolicy:
    """重试参数。**全部外置可配**（``C-1``）。"""

    #: 单个候选内的重试次数（不含首发）
    max_attempts_per_candidate: int = 2
    #: **跨候选累计**的上游调用上限 —— 故障放大器的开关
    total_max_attempts: int = 4
    backoff_base_s: float = 1.0
    jitter_ratio: float = 0.3

    def __post_init__(self) -> None:
        if self.total_max_attempts < 1:
            raise ValueError("total_max_attempts 必须 >= 1")
        # 这条校验放在构造期而不是留给运行时：总上限小于单候选上限，
        # 意味着第一个候选都跑不完预算就没了 —— 换候选的机制形同虚设。
        if self.total_max_attempts < self.max_attempts_per_candidate + 1:
            raise ValueError(
                f"total_max_attempts={self.total_max_attempts} 小于"
                f"（max_attempts_per_candidate+1={self.max_attempts_per_candidate + 1}）："
                "第一个候选就会耗尽全部预算，降级链永远走不到第二个候选"
            )

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> RetryPolicy:
        data = dict(cfg or {})
        return cls(
            max_attempts_per_candidate=int(data.get("max_attempts_per_candidate", 2)),
            total_max_attempts=int(data.get("total_max_attempts", 4)),
            backoff_base_s=float(data.get("backoff_base_s", 1.0)),
            jitter_ratio=float(data.get("jitter_ratio", 0.3)),
        )


def should_retry(error: BaseException, budget: CallBudget) -> bool:
    """该不该重试。

    两个条件是**与**关系，缺一不可：

    1. 错误本身标记为可重试（``ProviderError.retryable``）——
       401 / 400 / 404 重试只是白烧配额；
    2. 预算还没耗尽 —— **即使错误可重试**，也不能突破总上限。

    第 2 条容易被漏掉：只判 ``retryable`` 的话，总预算就形同虚设了。
    """
    return bool(getattr(error, "retryable", False)) and budget.exhausted_reason() is None


def compute_backoff(attempt: int, policy: RetryPolicy) -> float:
    """指数退避 + **抖动**。

    抖动不是可选项：多 agent 并发时，同步退避会让所有重试在同一时刻到达，
    把刚恢复的上游**再打挂一次**。抖动把这些请求摊开。

    Args:
        attempt: 第几次重试（从 0 开始），指数为 ``2 ** attempt``。
    """
    delay = policy.backoff_base_s * (2**max(0, attempt))
    if policy.jitter_ratio > 0:
        delay *= 1.0 + random.uniform(-policy.jitter_ratio, policy.jitter_ratio)
    return max(0.0, delay)

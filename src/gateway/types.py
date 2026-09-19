"""``src/gateway`` 的类型载体。

**为什么需要这个文件**（这是对目录结构的一处补充，见《架构概要设计-gateway》§8 D-A）：
``GatewayResult`` / ``AttemptRecord`` / ``ModelSpec`` / ``AliasSpec`` 若塞进 ``gateway.py``，
那个文件就要同时承载「类型定义」和「责任链编排」两件事 ——
而它本来就是全模块最复杂的一个文件。

**``CallBudget`` 刻意不在这里**：它是**行为**不是数据，且属于重试语义，
所以放在 ``retry.py``（同 D-A 的结论）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Union

from provider.types import Capability, ChatResponse, EmbeddingResult, Usage

__all__ = [
    "AliasSpec",
    "AttemptRecord",
    "AttemptOutcome",
    "Cost",
    "GatewayResult",
    "ModelSpec",
    "StreamChunk",
    "StreamDone",
    "StreamEvent",
    "StreamFailed",
]

AttemptOutcome = Literal["success", "failed", "skipped"]


@dataclass(frozen=True)
class ModelSpec:
    """一个物理模型的全部静态事实。

    ``capabilities`` 在注册时**定形**（配置声明 + 厂商默认合并后），
    调用路径上不再计算 —— 路由是热路径，不该有分支。
    """

    key: str
    provider: str
    model: str
    capabilities: frozenset[Capability] = frozenset()
    priority: int = 0
    weight: float | None = None
    #: 原始配置，交给 ``provider.build_provider`` 用
    config: Mapping[str, Any] = field(default_factory=dict)
    #: 注册期就失败的模型（如云厂商缺密钥）。**保留在注册表里**而不是丢掉 ——
    #: 丢掉会让「为什么这个候选没被用到」变成一个查不到的空缺。
    unavailable_reason: str | None = None

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def supports_all(self, capabilities: frozenset[Capability]) -> bool:
        return capabilities <= self.capabilities

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None


@dataclass(frozen=True)
class AliasSpec:
    """一个逻辑模型名（如 ``runtime.default``）。

    业务只认 ``alias``，物理模型由配置决定 —— 换模型不改业务代码（FR-G-02）。
    """

    alias: str
    candidates: tuple[str, ...]
    strategy: tuple[str, ...] = ("capability", "priority")

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError(f"alias {self.alias!r} 的 candidates 不能为空")


@dataclass(frozen=True)
class AttemptRecord:
    """一次尝试的结果。

    **保留全部尝试记录**（而非只留最终失败原因）是 ``FR-G-11`` 的要求：
    一次「m1 超时 → m2 限流 → m3 鉴权失败」的调用，只显示最后一条会掩盖真正的原因 ——
    而第一条往往才是导致后面连锁的那一个。
    """

    model_key: str
    provider: str = ""
    outcome: AttemptOutcome = "failed"
    #: 失败原因（已脱敏的字符串）。**不存异常对象** —— 它会被长期持有，可能泄漏。
    error: str | None = None
    retryable: bool | None = None
    #: 跳过原因：``circuit_open`` / ``rate_limited`` / ``budget_exhausted``
    skipped_reason: str | None = None
    elapsed_s: float = 0.0

    @property
    def skipped(self) -> bool:
        return self.outcome == "skipped"


@dataclass(frozen=True)
class Cost:
    """成本。``amount`` 为 ``None`` 表示**价格未知**，不是 0 元（FR-G-09）。"""

    currency: str = ""
    amount: Decimal | None = None

    @property
    def known(self) -> bool:
        return self.amount is not None

    def __str__(self) -> str:
        if self.amount is None:
            return "未知"
        return f"{self.amount} {self.currency}".strip()


@dataclass(frozen=True)
class GatewayResult:
    """一次 gateway 调用的完整结果。

    **``attempts`` 与 ``degraded`` 是必须暴露的**（FR-G-05）：
    静默降级会让「为什么这次答得差 / 花了更多钱」永远查不出来。
    """

    response: Union[ChatResponse, EmbeddingResult]
    alias: str
    model_key: str
    attempts: tuple[AttemptRecord, ...] = ()
    degraded: bool = False
    usage: Usage = field(default_factory=Usage)
    cost: Cost = field(default_factory=Cost)
    trace_id: str = ""

    @property
    def model(self) -> str:
        return getattr(self.response, "model", "")

    @property
    def content(self) -> str:
        """便捷取文本。仅对 ``ChatResponse`` 有意义。"""
        return getattr(self.response, "content", "")


# --------------------------------------------------------------------------- #
# 流式事件
# --------------------------------------------------------------------------- #

#: 流式无法在结束时「返回」一个值，所以把结果作为**事件**发出。
#: 这样调用方一个循环就能同时处理增量、结束与失败，而不必额外持有状态。


@dataclass(frozen=True)
class StreamChunk:
    """一段增量正文。"""

    text: str


@dataclass(frozen=True)
class StreamDone:
    """流正常结束，携带完整结果（含降级标记与成本）。"""

    result: GatewayResult


@dataclass(frozen=True)
class StreamFailed:
    """流以失败结束。

    **一旦发出过 :class:`StreamChunk`，失败就只能以本事件收场** ——
    正文已经吐给用户了，重新发起只会得到第二份不连贯的输出（FR-G-05）。
    """

    error: Exception
    attempts: tuple[AttemptRecord, ...] = ()


StreamEvent = Union[StreamChunk, StreamDone, StreamFailed]

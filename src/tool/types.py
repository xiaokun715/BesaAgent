"""``src/tool`` 的类型载体。

**这里的东西都不能依赖厂商协议** —— ``src/tool`` 不许 import ``provider``
（``pyproject.toml`` 的契约 4 机械强制）。工具定义是**本模块自己的类型**，
转换成厂商要的 ``ToolSpec`` 发生在组合根（见架构文档的 ``DT-5``）。

**``Outcome`` 刻意比「成功/失败」多几档**，因为下面这几件事必须能被区分：

============================  ==========================================
``executed``                  真的执行了，副作用发生了
``executed_degraded``         执行了，但**没有幂等保护**（Redis 不可用时的放行）
``reused``                    幂等命中，**副作用没有发生**
``uncertain``                 登记了开始、没等到结束 —— 副作用发生没有**不知道**
============================  ==========================================

合并成 ``ok: bool`` 的话，「这次到底有没有发生副作用」就再也答不出来了 ——
而那正是这个模块存在的理由。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "OUTCOME_ORDER",
    "Outcome",
    "SideEffect",
    "ToolDefinition",
    "ToolInvocation",
    "ToolResult",
]

#: 副作用等级。**由工具自己声明**，不是调用方猜的，也不是框架推的 ——
#: 框架推不出来（``bash`` 既可能是 ``ls`` 也可能是 ``rm -rf``），
#: 而它同时决定权限策略与 Redis 不可用时的降级行为。
SideEffect = Literal["read", "write", "destructive"]

#: 一次调用的结果档位。
Outcome = Literal[
    "executed",
    "executed_degraded",
    "reused",
    "in_flight",
    "refused",
    "failed",
    "uncertain",
]

#: 档位的"严重度"排序，供筛选与报表使用。
#: **不是成功度排序** —— ``reused`` 排在这里只是因为它排在 ``executed`` 之后
#: 读起来顺，没有任何语义。
OUTCOME_ORDER: tuple[Outcome, ...] = (
    "executed",
    "executed_degraded",
    "reused",
    "in_flight",
    "uncertain",
    "refused",
    "failed",
)

#: 这些档位表示「副作用确实发生过」。
SIDE_EFFECT_HAPPENED: frozenset[str] = frozenset({"executed", "executed_degraded"})

#: 这些档位表示「可以安全地再试一次」。
RETRYABLE: frozenset[str] = frozenset({"refused", "failed", "in_flight"})


@dataclass(frozen=True)
class ToolDefinition:
    """给模型看的工具说明。

    与 :class:`~tool.base.Tool` 分开是有意的：**定义是数据，工具是行为**。
    组合根需要把前者转成厂商的 ``ToolSpec``，而它不该拿到后者的执行入口。
    """

    name: str
    description: str = ""
    #: JSON Schema。**本模块不解析它**（不处理 ``$ref``），只做透传与顶层校验。
    parameters: Mapping[str, Any] = field(default_factory=dict)
    side_effect: SideEffect = "read"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("工具定义必须有名字")


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用的入参。

    ``scope`` 是幂等的一部分：**同一份参数在不同作用域里是不同的调用**。
    取 ``session_id`` 优先、缺省用运行标识 —— 但**不要用「进程」**，
    因为断点续跑会换进程，而那正是最需要去重的场合（架构文档 ``Q-1``）。
    """

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: 作用域。进幂等键，也进唯一约束
    scope: str = ""
    #: 调用方显式给的幂等键。给了就以它为准（`DT-1`）
    idempotency_key: str | None = None
    trace_id: str = ""
    session_id: str | None = None
    caller: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("工具名不能为空")


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。

    ``truncated`` **是必填字段而不是可选项**：截断而不标注，
    会让模型基于不完整的信息做判断，而它自己不知道 —— 那比报错更糟。
    """

    outcome: Outcome
    tool_name: str = ""
    output: str = ""
    #: 失败原因（已脱敏）。**不存异常对象** —— 与 ``AttemptRecord`` 同一条理由：
    #: 它会被长期持有并可能泄漏。
    error: str = ""
    #: 输出是否被截断
    truncated: bool = False
    elapsed_s: float = 0.0
    #: 本次实际使用的幂等键（便于与 ``tool_execution`` 表对上）
    idem_key: str = ""

    @property
    def ok(self) -> bool:
        """是否拿到了可用输出。**注意 ``reused`` 也算 ok** —— 它返回的是结果。"""
        return self.outcome in {"executed", "executed_degraded", "reused"}

    @property
    def side_effect_happened(self) -> bool:
        """这一次**真的**产生了副作用吗。

        与 :attr:`ok` 的区别在 ``reused`` 上：复用拿到了结果，但副作用没发生。
        「这次还要不要再跑一遍」这类判断必须看这个而不是 ``ok``。
        """
        return self.outcome in SIDE_EFFECT_HAPPENED

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        detail = self.error or f"{len(self.output)} 字符"
        return f"{mark} {self.tool_name} [{self.outcome}] {detail}"

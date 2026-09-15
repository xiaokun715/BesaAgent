"""路由：把候选集变成**有序的尝试链**。

策略是**管道式**的 —— 每个策略是 ``(候选集, 上下文) -> 候选集``，
按配置顺序依次应用。顺序即语义：``[capability, priority]`` 表示
「先剔除不满足能力的，再按优先级排序」。

**空结果的报错信息是本模块最重要的产出**（``FR-G-03``）。
候选全被过滤掉时，只说「没有可用模型」等于什么都没说 ——
必须指出**每个候选缺哪一项能力**，否则排查者只能去翻配置。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from gateway.errors import NoCapableModelError
from gateway.health import CircuitState, HealthRegistry
from gateway.types import ModelSpec
from provider.types import Capability

__all__ = ["RoutingContext", "STRATEGIES", "list_strategies", "select"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingContext:
    """路由所需的上下文。

    ``cost_of`` 与 ``health`` 只在对应策略被启用时才需要 ——
    做成可选字段，是为了让单元测试能只装配它关心的那一部分。
    """

    #: 本次请求必需的能力。由请求形态推导，不是调用方随手传的。
    required: frozenset[Capability] = frozenset()
    #: 用于 ``weight`` 策略的分流稳定性 —— 同一会话必须命中同一模型，
    #: 否则同一会话会在强弱模型之间跳变，体验不可解释（``D-F``）。
    session_id: str | None = None
    caller: str | None = None
    #: ``cost`` 策略取价格用
    cost_of: Callable[[str], Decimal | None] | None = None
    #: ``health`` 策略取健康度用
    health: HealthRegistry | None = None
    #: 人类可读的能力描述，仅用于错误信息（「流式 + 工具调用」）
    describes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def for_request(
        cls,
        *,
        stream: bool = False,
        tools: bool = False,
        vision: bool = False,
        json_output: bool = False,
        **extra: Any,
    ) -> RoutingContext:
        """从请求形态推导必需能力。

        **这是 ``required`` 的唯一来源** —— 让调用方自己传能力集，
        迟早在某个分支上漏传一项，然后那个分支就永远路由到不支持的模型。
        """
        required: set[Capability] = {Capability.CHAT}
        labels: list[str] = []
        if stream:
            required.add(Capability.STREAM)
            labels.append("流式")
        if tools:
            required.add(Capability.TOOLS)
            labels.append("工具调用")
        if vision:
            required.add(Capability.VISION)
            labels.append("视觉")
        if json_output:
            required.add(Capability.JSON)
            labels.append("结构化输出")
        return cls(required=frozenset(required), describes=tuple(labels), **extra)


# --------------------------------------------------------------------------- #
# 策略
# --------------------------------------------------------------------------- #


def _by_capability(specs: list[ModelSpec], ctx: RoutingContext) -> list[ModelSpec]:
    if not ctx.required:
        return specs
    return [spec for spec in specs if spec.supports_all(ctx.required)]


def _by_priority(specs: list[ModelSpec], ctx: RoutingContext) -> list[ModelSpec]:
    # 稳定排序：同优先级保持配置里的原始顺序 —— 配置顺序是用户表达偏好的方式，
    # 被排序打乱会让「我把 A 写在前面」这个意图失效。
    return sorted(specs, key=lambda spec: -spec.priority)


def _by_weight(specs: list[ModelSpec], ctx: RoutingContext) -> list[ModelSpec]:
    """按权重分流，且**对同一会话稳定**。

    用 Efraimidis–Spirakis 的加权随机采样：为每个候选算
    ``u ** (1/w)``（``u`` 由会话与模型键确定性派生），取最大的。
    ``u`` 的确定性保证同一会话每次都选中同一个模型；
    ``1/w`` 的指数保证权重大的模型更常排在前面。
    """
    weighted = [spec for spec in specs if spec.weight]
    if not weighted:
        return specs

    session = ctx.session_id or ""

    def score(spec: ModelSpec) -> float:
        weight = spec.weight or 1.0
        if weight <= 0:
            return -1.0
        uniform = _stable_uniform(session, spec.key)
        return uniform ** (1.0 / weight)

    return sorted(specs, key=score, reverse=True)


def _by_cost(specs: list[ModelSpec], ctx: RoutingContext) -> list[ModelSpec]:
    """便宜的优先。**价格未知的排最后**，而不是当成 0 排最前 ——
    后者会让「没配价格」的模型独占流量，然后在账单上给你一个「未知成本」。"""
    if ctx.cost_of is None:
        return specs

    def key(spec: ModelSpec) -> tuple[int, Decimal]:
        price = ctx.cost_of(spec.key)
        if price is None:
            return (1, Decimal(0))
        return (0, price)

    return sorted(specs, key=key)


def _by_health(specs: list[ModelSpec], ctx: RoutingContext) -> list[ModelSpec]:
    """健康的优先。

    注意这只影响**排序**；**硬拦截**由 ``CircuitBreaker.allow()`` 负责
    （架构概要设计-gateway §4.3 的 B-5）。两者都存在是刻意的：
    排序让健康的模型优先，硬拦截保证熔断的模型不被浪费预算。
    """
    if ctx.health is None:
        return specs

    order = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 1,   # 半开：可以试，但排后面
        CircuitState.OPEN: 2,
    }
    return sorted(specs, key=lambda spec: order[ctx.health.breaker(spec.key).state])


#: 策略名 → 实现。配置里写错名字会在**注册表构建期**报错（``C-3``），
#: 而不是等第一次线上调用才发现路由没生效。
STRATEGIES: dict[str, Callable[[list[ModelSpec], RoutingContext], list[ModelSpec]]] = {
    "capability": _by_capability,
    "priority": _by_priority,
    "weight": _by_weight,
    "cost": _by_cost,
    "health": _by_health,
}


def list_strategies() -> tuple[str, ...]:
    return tuple(STRATEGIES)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def select(
    candidates: Sequence[ModelSpec],
    ctx: RoutingContext,
    strategy: Sequence[str] = ("capability", "priority"),
) -> list[ModelSpec]:
    """应用策略管道，产出有序尝试链。

    Raises:
        NoCapableModelError: 过滤后为空。错误信息会指出**每个候选缺什么** ——
            这是本函数存在的主要理由：让配置错误在第一次调用时就自解释。
        ValueError: 策略名未知（正常应在注册表构建期就被拦下）。
    """
    available = [spec for spec in candidates if spec.available]
    unavailable = [spec for spec in candidates if not spec.available]

    chain = list(available)
    for name in strategy:
        implementation = STRATEGIES.get(name)
        if implementation is None:
            known = ", ".join(sorted(STRATEGIES))
            raise ValueError(f"未知的路由策略 {name!r}；已知策略：{known}")
        chain = implementation(chain, ctx)

    if chain:
        return chain

    raise NoCapableModelError(
        _explain(candidates, unavailable, ctx),
        alias="",
    )


def _explain(
    candidates: Sequence[ModelSpec],
    unavailable: Sequence[ModelSpec],
    ctx: RoutingContext,
) -> str:
    """构造一份**能直接照着改配置**的错误信息。"""
    wanted = " + ".join(ctx.describes) or "对话"
    lines = [f"没有任何候选满足本次请求的能力要求（{wanted}）"]

    for spec in candidates:
        if not spec.available:
            # 注册期就失败的模型：原因与能力无关，单独说，
            # 否则会把「缺密钥」误报成「缺能力」，排查方向直接跑偏。
            lines.append(f"  - {spec.key}：注册期不可用（{spec.unavailable_reason}）")
            continue
        missing = sorted(cap.value for cap in (ctx.required - spec.capabilities))
        if missing:
            lines.append(f"  - {spec.key}：缺少 {', '.join(missing)}")
        else:
            lines.append(f"  - {spec.key}：满足能力，被后续策略过滤")

    if unavailable and not candidates:
        lines.append("（候选集为空：请检查 alias 的 candidates 是否引用了已注册的模型）")
    return "\n".join(lines)


def _stable_uniform(session: str, key: str) -> float:
    """由 (会话, 模型键) 确定性派生一个 (0, 1] 的均匀随机数。

    用哈希而不是 ``random``：分流必须**可复现** ——
    出了问题时能靠「哪个会话」重算出同一个选择，否则「为什么这个会话用了贵模型」
    永远无法回答。
    """
    digest = hashlib.sha256(f"{session}::{key}".encode("utf-8")).digest()
    # 取 8 字节转成 (0, 1] —— 加 1 避免 0（0 ** x 恒为 0，会让权重失效）
    raw = int.from_bytes(digest[:8], "big")
    return (raw + 1) / (2**64 + 1)

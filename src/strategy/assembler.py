"""装配：把「选中了哪些策略」固化成一份 **可传递、可留痕的配置**（`FR-ST-15`）。

**它不做装配的另一半**：把 profile 变成真正的组件实例，是**组合根**的事
（与 `DT-5`「工具定义 → `ToolSpec` 的转换在组合根」完全同构）。
本文件只负责"把决策写成一份配置"，所以它**没有任何 IO、没有工厂、不认识组件**。

**版本在这里落定**：profile 的 ``versions`` 从注册表读当次声明的版本 ——
它是评测结论能不能被复用的唯一依据（`FR-ST-11`），所以必须由**这里**统一填，
不能让调用方自己拼（拼错了会让一条旧结论错误地被复用，而那不会被怀疑）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from strategy.registry import StrategyRegistry
from strategy.types import (
    COMPONENT_KINDS,
    AgentProfile,
    ComponentKind,
    RiskLevel,
    SelectionDecision,
)

__all__ = ["assemble", "build_profile"]


def build_profile(
    *,
    name: str,
    bindings: Mapping[ComponentKind, str],
    registry: StrategyRegistry,
    layer: str = "default",
    reason: str = "",
    risk: RiskLevel = "low",
    params: Mapping[str, Any] | None = None,
) -> AgentProfile:
    """按绑定表造一份 profile，并**从注册表读出每个策略的当前版本**。

    未知的 ``组件.策略`` 会**硬失败**（经 ``registry.get``）——
    枚举与选择都可能拼错名字，而拼错的后果是"这套配置在评测里永远查不到数据"，
    一个不会报错的死胡同。
    """
    resolved: dict[ComponentKind, str] = {}
    versions: dict[str, str] = {}
    for kind in COMPONENT_KINDS:
        strategy = str(bindings.get(kind, "") or "")
        if not strategy:
            continue
        spec = registry.get(kind, strategy)   # 未知 → 抛 UnknownStrategyError（含全部可用名）
        resolved[kind] = strategy
        versions[spec.key] = spec.version
    return AgentProfile(
        name=name,
        bindings=resolved,
        versions=versions,
        params=dict(params or {}),
        layer=layer,  # type: ignore[arg-type]
        reason=reason,
        risk=risk,
    )


def assemble(decision: SelectionDecision, *, registry: StrategyRegistry) -> AgentProfile:
    """把一次选择固化成 profile。

    ``decision.profile`` 已经带了绑定表（选择器为了跑契约校验必须先生成它），
    这里做的是**补上决策侧的信息**：走的哪一层、为什么、风险等级 ——
    `FR-ST-15` 要求产出必须自解释，而"自解释"指的就是这几项。
    """
    if decision.profile is None:
        raise ValueError(
            "这次决策没有选中任何组合，无法装配。"
            "（调用方应当先看 decision.refused / decision.degraded —— "
            "拒绝与降级都应当已经给出 profile 或说明原因，走到这里是编程错误。）"
        )
    base = decision.profile
    return build_profile(
        name=base.name,
        bindings=dict(base.bindings),
        registry=registry,
        layer=decision.layer,
        reason=decision.reason,
        risk=base.risk,
        params=dict(base.params),
    )

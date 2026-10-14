"""策略注册表（`FR-ST-04` / `FR-ST-05`）。

**它只存声明，不存实现** —— 表里没有工厂函数，而且**刻意不让它有机会有**：
只要 ``register`` 的签名里带上了工厂，就会有人忍不住在 ``selector`` 里
"顺手启一个组件试试"，那一刻这一层就开始执行了（`NFR-ST-02` 就破了）。
工厂住在组合根（架构文档 §6）。

**两个键的纪律**：

- 键是 ``(组件类别, 策略名)`` 而不是单个名字 —— 因为 ``sliding_window`` 在
  上下文与记忆里都可能有，单名键会把它们撞在一起；
- 重名**硬失败**：静默覆盖会让一条策略悄悄失效，而表现是"某些任务突然换了做法"。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from strategy.types import COMPONENT_KINDS, ComponentKind, StrategySpec

__all__ = ["StrategyRegistry", "UnknownStrategyError"]


class UnknownStrategyError(KeyError):
    """未知的策略。

    **错误信息里列出该类别下的全部策略名** —— 与 ``FR-G-02`` 的逻辑名寻址、
    ``agent`` 的阶段名同一条纪律：拼错是最常见的错误，只说「未知」会让人去翻文档。
    """

    def __init__(self, kind: ComponentKind, name: str, known: tuple[str, ...]) -> None:
        self.kind = kind
        self.name = name
        self.known = known
        super().__init__(
            f"组件 {kind!r} 下没有策略 {name!r}；该类别已登记："
            f"{', '.join(known) or '（无）'}"
        )


class StrategyRegistry:
    """策略声明表。**只登记、只查找，不做任何判断。**"""

    __slots__ = ("_specs",)

    def __init__(self, specs: Iterable[StrategySpec] = ()) -> None:
        self._specs: dict[tuple[str, str], StrategySpec] = {}
        for spec in specs:
            self.register(spec)

    # ---------------------------------------------------------------- 写
    def register(self, spec: StrategySpec) -> None:
        key = (spec.kind, spec.name)
        existing = self._specs.get(key)
        if existing is not None:
            # 静默覆盖会让其中一条悄悄失效 —— 而它的下游是"这个任务怎么老失败"。
            raise ValueError(
                f"策略 {spec.kind}.{spec.name} 重复注册"
                f"（已有一条 @{existing.version}，本次是 @{spec.version}）。\n"
                "覆盖会让其中一条悄悄失效，所以这里硬失败。"
                "同名但不同组件的策略是允许的 —— 键是（组件, 策略名）二元组。"
            )
        self._specs[key] = spec

    def mark_unavailable(self, kind: ComponentKind, name: str, reason: str) -> None:
        """把一条策略标成不可用，**保留在册**（`FR-ST-05`）。

        **不删**：删掉之后"为什么不能用它"就变成一个查不到的空缺；
        保留 + 原因，答案是一句能照做的话（「multiagent 未启用」）。

        前置条件在启动期判定（例如发现 ``multiagent`` 没启用），
        判定结果**写在表里**而不是散在各调用点 —— 否则「哪些不可用」会有多个说法。
        """
        spec = self.get(kind, name)
        if not reason.strip():
            raise ValueError(f"标记 {kind}.{name} 不可用必须给原因")
        self._specs[(kind, name)] = StrategySpec(
            kind=spec.kind,
            name=spec.name,
            version=spec.version,
            interface=spec.interface,
            inputs=spec.inputs,
            provides=spec.provides,
            preconditions=spec.preconditions,
            cost=spec.cost,
            scenarios=spec.scenarios,
            metrics=spec.metrics,
            available=False,
            unavailable_reason=reason,
        )

    # ---------------------------------------------------------------- 读
    def get(self, kind: ComponentKind, name: str) -> StrategySpec:
        spec = self._specs.get((kind, name))
        if spec is None:
            raise UnknownStrategyError(kind, name, tuple(sorted(
                n for (k, n) in self._specs if k == kind
            )))
        return spec

    def has(self, kind: ComponentKind, name: str) -> bool:
        return (kind, name) in self._specs

    def by_kind(self, kind: ComponentKind) -> tuple[StrategySpec, ...]:
        """该类别下的全部策略（含不可用的），**按名字排序**以满足确定性。"""
        return tuple(
            self._specs[(kind, name)]
            for name in sorted(n for (k, n) in self._specs if k == kind)
        )

    def available(self, kind: ComponentKind) -> tuple[StrategySpec, ...]:
        """该类别下**可用**的策略。"""
        return tuple(spec for spec in self.by_kind(kind) if spec.available)

    def all(self) -> tuple[StrategySpec, ...]:
        return tuple(
            self._specs[(kind, name)]
            for kind in COMPONENT_KINDS
            for name in sorted(n for (k, n) in self._specs if k == kind)
        )

    def to_dict(self) -> Mapping[str, Any]:
        """给 ``doctor`` 用：七个组件、每个下面的可用与不可用，不可用带原因（`CST-3`）。"""
        return {
            kind: [
                {
                    "name": spec.name,
                    "version": spec.version,
                    "available": spec.available,
                    "reason": spec.unavailable_reason,
                    "scenarios": list(spec.scenarios or ()),
                }
                for spec in self.by_kind(kind)
            ]
            for kind in COMPONENT_KINDS
        }

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, tuple) and len(key) == 2 and key in self._specs

    def __repr__(self) -> str:
        return f"StrategyRegistry({len(self._specs)} specs)"

"""组件契约：**哪些拼法根本不能成立**（`FR-ST-06` / `FR-ST-07`）—— 本层的核心价值。

**为什么冲突必须在装配之前挡住**：那四类冲突的共同形态是
「每个组件各自正常，任务整体失败」—— 启动不报错、组件里也不报错，
只表现为「这个任务怎么老失败」。

    校验的成本：一次纯函数调用。
    不校验的成本：一轮真实的模型调用 + 可能已发生副作用 + 排查时间。

两者的差距就是这一层存在的理由（架构文档 §3.1）。

**规则是开放登记的**：``Contract`` 是一条规则 + 一个纯函数，
新增一条约束**不改校验器本身**（与 `FR-A-01` 的"新增阶段只加数据"同一条纪律）。

**每条规则的 ``detail`` 都要写后果**，而不是"不兼容"三个字 ——
与 tool 层"拒绝时说清缺哪个权限"、gateway"未知 alias 要列出全部"是同一条纪律：
错误信息的质量直接决定这一轮是「人自己改对」还是「来查文档」。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from strategy.assembler import build_profile
from strategy.registry import StrategyRegistry
from strategy.types import (
    COMPONENT_KINDS,
    COMPONENT_LABELS,
    AgentProfile,
    ComponentKind,
    StrategySpec,
    Violation,
    fingerprint_sequence,
)

__all__ = [
    "Contract",
    "default_contracts",
    "enumerate_valid",
    "enumerate_valid_with_stats",
]

#: 一条规则：名字 + **纯函数**。无 IO、无时间、无随机 ——
#: 所以它可以在单测里穷举（`NFR-ST-04`）。
Contract = Callable[[AgentProfile], tuple[Violation, ...]]


@dataclass(frozen=True)
class _Named:
    """带名字的规则。``Contract`` 是函数本身，这里补上它叫什么。"""

    name: str
    check: Contract

    def __call__(self, profile: AgentProfile) -> tuple[Violation, ...]:
        return self.check(profile)


def _label(kind: ComponentKind) -> str:
    return COMPONENT_LABELS.get(kind, str(kind))


def _bound(profile: AgentProfile, kind: ComponentKind) -> str:
    return str(profile.bindings.get(kind, "") or "")


def _provides(registry: StrategyRegistry, kind: ComponentKind, name: str) -> frozenset[str]:
    if not name:
        return frozenset()
    return registry.get(kind, name).provides


def _inputs(registry: StrategyRegistry, kind: ComponentKind, name: str) -> frozenset[str]:
    if not name:
        return frozenset()
    return registry.get(kind, name).inputs


def _all_provided(profile: AgentProfile, registry: StrategyRegistry) -> frozenset[str]:
    """整个组合**联合起来**能提供什么。"""
    provided: set[str] = set()
    for kind in COMPONENT_KINDS:
        provided |= set(_provides(registry, kind, _bound(profile, kind)))
    return frozenset(provided)


# --------------------------------------------------------------------------- #
# 五条内置规则（`FR-ST-06`）
# --------------------------------------------------------------------------- #


def _readonly_blocks_write_tools(
    registry: StrategyRegistry,
) -> Contract:
    """只读权限 × 需要写入的工具（`FR-ST-06` 第一条）。

    用**声明式**判据而不是硬编码策略名：工具选择策略声明它需要 ``write_access``，
    权限策略在提供 ``write_access`` 时才算放行。这样新增一条权限档位或工具选择策略
    时，这条规则**不需要改**。
    """

    def check(profile: AgentProfile) -> tuple[Violation, ...]:
        needed = _inputs(registry, "tool_selection", _bound(profile, "tool_selection"))
        if "write_access" not in needed:
            return ()
        permission = _bound(profile, "permission")
        if "write_access" in _provides(registry, "permission", permission):
            return ()
        return (
            Violation(
                rule="readonly_blocks_write_tools",
                components=("permission", "tool_selection"),
                detail=(
                    f"权限档位「{permission}」不提供写权限，而工具选择「"
                    f"{_bound(profile, 'tool_selection')}」需要它 —— "
                    "**工具必然被拒**，而 agent 会反复重试它，"
                    "直到撞上步数上界。失败发生在离原因非常远的地方。"
                ),
                suggestion=(
                    "把权限档位换成提供 write_access 的那一档，"
                    "或把工具选择换成只用只读工具的固定集。"
                ),
            ),
        )

    return check


def _state_compression_loses_memory_fields(
    registry: StrategyRegistry,
) -> Contract:
    """任务状态压缩 × 结构化经验记忆（`FR-ST-06` 第二条）。

    上下文策略**没提供**记忆需要的那个字段 → 记忆会拿到残缺结构。
    """

    def check(profile: AgentProfile) -> tuple[Violation, ...]:
        memory = _bound(profile, "memory")
        needed = _inputs(registry, "memory", memory)
        if not needed:
            return ()
        provided = _all_provided(profile, registry)
        missing = needed - provided
        if not missing:
            return ()
        return (
            Violation(
                rule="state_compression_loses_memory_fields",
                components=("context", "memory"),
                detail=(
                    f"记忆机制「{memory}」需要 {'、'.join(sorted(missing))}，"
                    f"而当前上下文策略「{_bound(profile, 'context')}」没有提供它 —— "
                    "**存进去的是残缺经验**（字段缺了，记忆本身不会报错，"
                    "只会在下次复用这条经验时给出错误的判断）。"
                ),
                suggestion=(
                    "把上下文策略换成保留任务状态的（如保留 task_state），"
                    "或把记忆机制换成不依赖这些字段的那一档。"
                ),
            ),
        )

    return check


def _cross_validation_needs_multiagent(
    registry: StrategyRegistry,
) -> Contract:
    """多 Agent 交叉验证 × 依赖不可用（`FR-ST-06` 第三条）。

    覆盖**所有**被标成不可用的策略 —— 不只是交叉验证那一条：
    「验证会安静地退化」只是这类问题里最典型的一个，
    任何不可用策略被选进组合，后果都是同一种"安静地少做了一件事"。
    """

    def check(profile: AgentProfile) -> tuple[Violation, ...]:
        violations: list[Violation] = []
        for kind in COMPONENT_KINDS:
            name = _bound(profile, kind)
            if not name:
                continue
            spec: StrategySpec = registry.get(kind, name)
            if spec.available:
                continue
            violations.append(
                Violation(
                    rule="cross_validation_needs_multiagent"
                    if spec.preconditions
                    else "unavailable_strategy",
                    components=(kind,),
                    detail=(
                        f"{_label(kind)}的策略「{name}」当前**不可用**："
                        f"{spec.unavailable_reason}。\n"
                        f"它需要：{'；'.join(spec.preconditions or ()) or '（未声明前置条件）'} —— "
                        "选中它不会报错，只会**安静地少做那一件事**"
                        "（例如验证退化成一个 agent 的结论）。"
                    ),
                    suggestion=(
                        "换一条可用的策略；或者在依赖具备之前，"
                        "把这一档显式降级（而不是让它在组合里挂着）。"
                    ),
                )
            )
        return tuple(violations)

    return check


def _compare_needs_multiple_candidates(
    registry: StrategyRegistry,
) -> Contract:
    """候选方案比较 × 单次执行（`FR-ST-06` 第四条）。

    **这套组合没有意义，但不报错** —— 比较需要多个候选，而单次执行只产出一个。
    """

    def check(profile: AgentProfile) -> tuple[Violation, ...]:
        execution = _bound(profile, "execution")
        needed = _inputs(registry, "execution", execution)
        if "multiple_candidates" not in needed:
            return ()
        if "multiple_candidates" in _all_provided(profile, registry):
            return ()
        return (
            Violation(
                rule="compare_needs_multiple_candidates",
                components=("execution",),
                detail=(
                    f"执行策略「{execution}」要比较多个候选，"
                    "但当前组合里没有任何组件产出第二个候选 —— "
                    "**这套组合没有意义**：它会去「比较」一个只有一个元素的集合，"
                    "而结果看起来完全正常（就是那个唯一的候选）。"
                ),
                suggestion=(
                    "换成单次执行，或补上能产出多候选的那一档"
                    "（如并行的候选生成）。"
                ),
            ),
        )

    return check


def _stateless_conflicts_with_structured_memory(
    registry: StrategyRegistry,
) -> Contract:
    """无记忆 × 别处要记忆内容（`FR-ST-06` 第五条）—— 记忆机制内部矛盾。

    同一个组件只能绑一条策略，所以这条规则抓的是**跨组件的矛盾**：
    「不记」的那一档被选上，而另一个组件声明它需要记忆内容。
    """

    def check(profile: AgentProfile) -> tuple[Violation, ...]:
        memory = _bound(profile, "memory")
        if not memory:
            return ()
        if "memory_content" in _provides(registry, "memory", memory):
            return ()
        consumers = [
            kind
            for kind in COMPONENT_KINDS
            if kind != "memory"
            and "memory_content" in _inputs(registry, kind, _bound(profile, kind))
        ]
        if not consumers:
            return ()
        return (
            Violation(
                rule="stateless_conflicts_with_structured_memory",
                components=("memory", *consumers),
                detail=(
                    f"记忆机制「{memory}」不产出记忆内容，"
                    f"而 {'、'.join(_label(c) for c in consumers)} 依赖它 —— "
                    "同一套组合不能既是「不记」又是「记得最细」。\n"
                    "⚠ 这条最常见的成因不是配错，而是**降级时把记忆换成了无状态那一档**，"
                    "而其余组件还停在原处。"
                ),
                suggestion="要么给记忆换回产出记忆内容的那一档，要么把依赖它的组件也一并降级。",
            ),
        )

    return check


def default_contracts(registry: StrategyRegistry) -> tuple[_Named, ...]:
    """一期内置的五条规则（`FR-ST-06`）。

    **顺序即报告顺序** —— 一次可能同时违反多条，报告按这个顺序读起来最顺
    （先权限这种"必然失败"的，再记忆这种"安静出错"的）。
    """
    return (
        _Named("readonly_blocks_write_tools", _readonly_blocks_write_tools(registry)),
        _Named("state_compression_loses_memory_fields",
               _state_compression_loses_memory_fields(registry)),
        _Named("cross_validation_needs_multiagent",
               _cross_validation_needs_multiagent(registry)),
        _Named("compare_needs_multiple_candidates",
               _compare_needs_multiple_candidates(registry)),
        _Named("stateless_conflicts_with_structured_memory",
               _stateless_conflicts_with_structured_memory(registry)),
    )


def validate(profile: AgentProfile, contracts: Sequence[Contract]) -> tuple[Violation, ...]:
    """跑一遍全部规则，收集违反项（**不短路**：一次把所有问题都报出来）。"""
    violations: list[Violation] = []
    for contract in contracts:
        violations.extend(contract(profile))
    return tuple(violations)


# --------------------------------------------------------------------------- #
# 枚举（`FR-ST-07` / `NFR-ST-08`）
# --------------------------------------------------------------------------- #


def enumerate_valid_with_stats(
    registry: StrategyRegistry,
    contracts: Sequence[Contract],
    *,
    kinds: Sequence[ComponentKind] = COMPONENT_KINDS,
    limit: int | None = None,
) -> tuple[tuple[AgentProfile, ...], Mapping[str, int]]:
    """枚举全部**合法**组合，并返回一个统计。

    朴素做法就是**笛卡尔积再过滤**（架构文档 §3.4）：一期每类 2–5 个策略，
    乘积在几千量级，够用。

    ⚠ ``limit`` **只限制"返回多少个"，不限制"检查了多少个"**。
    这一点是刻意的：`total` 与 `valid` 是**约束松紧的唯一观测指标**（`MST-12`）——
    为了省事只检查前 N 个，会让那个指标变成谎话，而"约束没写紧"这件事
    就再也看不见了。所以统计永远是真的，截断数单独报出来。
    """
    pools = [registry.available(kind) for kind in kinds]
    empty = [kind for kind, pool in zip(kinds, pools) if not pool]
    if empty:
        # 某一类下一条可用策略都没有 —— 空集，而不是"配不出来"。
        return (), {"total": 0, "valid": 0, "empty_kinds": len(empty), "returned": 0,
                    "truncated": 0}

    valid: list[AgentProfile] = []
    total = 0
    for combo in itertools.product(*pools):
        total += 1
        bindings = {spec.kind: spec.name for spec in combo}
        profile = build_profile(
            name="enumerated", bindings=bindings, registry=registry, layer="rules",
        )
        if not validate(profile, contracts):
            valid.append(profile)

    returned = valid if limit is None else valid[: max(0, limit)]
    stats = {
        "total": total,
        "valid": len(valid),
        "empty_kinds": 0,
        "returned": len(returned),
        "truncated": len(valid) - len(returned),
    }
    return tuple(returned), stats


def enumerate_valid(
    registry: StrategyRegistry,
    contracts: Sequence[Contract],
    *,
    kinds: Sequence[ComponentKind] = COMPONENT_KINDS,
    limit: int | None = None,
) -> tuple[AgentProfile, ...]:
    """只要合法组合（`FR-ST-07`）。

    **结果按 ``profile_key`` 排序** —— 枚举要"可重复"（`CST-9`），
    而 ``itertools.product`` 的顺序依赖注册表的排序，多一层显式排序更稳。
    """
    profiles, _stats = enumerate_valid_with_stats(
        registry, contracts, kinds=kinds, limit=limit
    )
    ordered = sorted(profiles, key=lambda p: p.profile_key)
    return tuple(ordered)


def combos_fingerprint(profiles: Sequence[AgentProfile]) -> str:
    """枚举结果指纹 —— `CST-9` 的"可重复"就靠它断言。"""
    return fingerprint_sequence([p.profile_key for p in profiles])

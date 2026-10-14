"""``src/strategy`` 的契约载体。

**这一层的全部意义都在"声明"上**，所以类型定义得比别处更克制：

- :class:`StrategySpec` 是**一份声明**，不是实现 —— 它回答「这条策略是什么、要什么、
  产出什么、什么时候成立、大概多贵、擅长什么」。**六项缺一不可**（`FR-ST-02`）；
- :class:`AgentProfile` 是**一份配置**，不是实例 —— 它是"这次任务用哪套做法"的
  固化物，要能序列化、能进评测记录、能回答"它是怎么被选出来的"（`FR-ST-15`）。

**成本与质量指标是声明值，允许不准**，但不允许不填：不填就没法被比较，
而本模块的价值全在比较与选择上；填了不准，评测数据会把它纠正回来
（架构文档 §2.2）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "COMPONENT_KINDS",
    "COMPONENT_LABELS",
    "SELECTION_LAYERS",
    "AgentProfile",
    "Candidate",
    "ComponentKind",
    "CostEstimate",
    "EvalRecord",
    "Layer",
    "RiskLevel",
    "SelectionDecision",
    "StrategySpec",
    "TaskFeatures",
    "Violation",
    "fingerprint_sequence",
    "parse_profile_key",
]

#: 七个组件类别。**固定**（`FR-ST-01`）—— 它是本层的骨架，不该随手加。
#:
#: 顺序即枚举顺序：`enumerate_valid` 按它做笛卡尔积，所以顺序变了枚举结果的
#: 排列也会变（不影响集合，只影响输出的稳定性）。
ComponentKind = Literal[
    "context",
    "memory",
    "retrieval",
    "tool_selection",
    "permission",
    "validation",
    "execution",
]

COMPONENT_KINDS: tuple[ComponentKind, ...] = (
    "context",
    "memory",
    "retrieval",
    "tool_selection",
    "permission",
    "validation",
    "execution",
)

#: 组件的中文名。**给人看的**，也用于错误信息 —— 报错时写 `permission` 还是
#: 写「权限管理」，决定读的人要不要再去查一次类别名。
COMPONENT_LABELS: Mapping[str, str] = {
    "context": "上下文管理",
    "memory": "记忆机制",
    "retrieval": "检索策略",
    "tool_selection": "工具选择",
    "permission": "权限管理",
    "validation": "验证机制",
    "execution": "执行策略",
}

#: 任务的风险等级。**高风险是硬约束，不是权重**（`FR-ST-08`）。
RiskLevel = Literal["low", "high"]

#: 一次选择走的是哪一层（`FR-ST-10` 的四层 + 一个降级终点）。
#:
#: 它必须进事件与选择记录 —— 否则「同一个任务，昨天好好的，今天为什么换了套配置」
#: 这个问题没有答案（架构文档 §8）。
Layer = Literal["template", "rules", "evaluated", "default"]

SELECTION_LAYERS: tuple[Layer, ...] = ("template", "rules", "evaluated", "default")


@dataclass(frozen=True)
class CostEstimate:
    """量级即可。**它是声明值** —— 真实值由评测数据给出（架构文档 §2.2）。"""

    tokens: int = 0
    latency_ms: int = 0
    external_calls: int = 0

    def to_dict(self) -> Mapping[str, int]:
        return {"tokens": self.tokens, "latency_ms": self.latency_ms,
                "external_calls": self.external_calls}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CostEstimate":
        data = dict(data or {})
        return cls(
            tokens=int(data.get("tokens", 0)),
            latency_ms=int(data.get("latency_ms", 0)),
            external_calls=int(data.get("external_calls", 0)),
        )


@dataclass(frozen=True)
class StrategySpec:
    """一条策略的**声明**（`FR-ST-02` 的六要素）。

    ⚠ **六项缺一不可，且"缺"与"空"是两回事**：``preconditions=()`` 表示
    「无条件成立」，那是**填了**；而 ``preconditions=None`` 表示**根本没写**，
    构造时直接硬失败。用 ``None`` 与空容器区分这两者，是为了让
    `CST-1`（登记一条缺"成本"的策略 → 硬失败）真的可机械校验 ——
    若给 ``CostEstimate()`` 一个默认值，"没填成本"就永远检测不出来。
    """

    kind: ComponentKind
    name: str
    version: str
    #: 它满足哪一个组件契约（如 ``ContextComponent``）
    interface: str | None = None
    #: 它需要什么（进契约校验）
    inputs: frozenset[str] = frozenset()
    #: 它产出什么（进契约校验）
    provides: frozenset[str] = frozenset()
    #: 什么情况下才成立（``()`` = 无条件）
    preconditions: tuple[str, ...] | None = None
    #: 资源成本量级
    cost: CostEstimate | None = None
    #: 擅长 / 不擅长什么（给人看，**也是规则层的依据**）
    scenarios: tuple[str, ...] | None = None
    #: 用什么衡量它做得好
    metrics: Mapping[str, str] | None = None
    #: 前置条件不满足时**保留在册**并带上原因（`FR-ST-05`）
    available: bool = True
    unavailable_reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.name).strip():
            raise ValueError("策略声明必须有 name")
        # **六项缺一不可**（`FR-ST-02` / `CST-1`）。错误信息要指出**缺哪一项** ——
        # 只说"声明不完整"会让人对着文档逐项核。
        missing = [
            label
            for label, value in (
                ("接口 interface", self.interface),
                ("版本 version", self.version),
                ("前置条件 preconditions", self.preconditions),
                ("成本 cost", self.cost),
                ("适用场景 scenarios", self.scenarios),
                ("质量指标 metrics", self.metrics),
            )
            if value is None or (isinstance(value, str) and not value.strip())
        ]
        if missing:
            raise ValueError(
                f"策略 {self.kind}.{self.name} 的声明缺了：{'、'.join(missing)}。\n"
                "**六项缺一不可**：没有这六项的不是策略，只是开关 —— "
                "开关能配上，但没法被比较，而这一层的价值全在比较与选择上。\n"
                "（「无条件成立」请显式写 `preconditions=()`，那是填了；不写才是缺。）"
            )
        if not self.available and not self.unavailable_reason.strip():
            # 与 `ModelSpec.unavailable_reason` 同一条纪律：说"不可用"却不说为什么，
            # 等于把问题留给下一个读代码的人。
            raise ValueError(
                f"策略 {self.kind}.{self.name} 标了不可用，但没给原因。\n"
                "原因要具体（「multiagent 未启用」而不是「不可用」）—— "
                "「为什么不能用它」是排障时最常见的问题之一。"
            )

    @property
    def key(self) -> str:
        """``组件.策略`` —— 进 profile、进评测记录、进事件的那个键。"""
        return f"{self.kind}.{self.name}"

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "interface": self.interface,
            "inputs": sorted(self.inputs),
            "provides": sorted(self.provides),
            "preconditions": list(self.preconditions or ()),
            "cost": (self.cost or CostEstimate()).to_dict(),
            "scenarios": list(self.scenarios or ()),
            "metrics": dict(self.metrics or {}),
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }

    def __str__(self) -> str:
        mark = "" if self.available else f"（不可用：{self.unavailable_reason}）"
        return f"{self.key}@{self.version}{mark}"


@dataclass(frozen=True)
class TaskFeatures:
    """选择的**唯一输入**（`FR-ST-09`）。**字段名是契约**，不许中途改名。"""

    task_type: str = ""
    risk: RiskLevel = "low"
    budget_tokens: int | None = None
    budget_latency_ms: int | None = None
    budget_cost: float | None = None
    environment: str = ""
    #: 输入资产的集合（如 ``{"has_test_cases"}``）—— 模板裁剪按它判断（`FR-ST-14`）
    input_assets: frozenset[str] = frozenset()
    #: 显式指定模板名（给了就跳过匹配）。留空 = 按 ``task_type`` 匹配。
    template: str = ""

    @property
    def template_key(self) -> str:
        """用哪个模板名去匹配。"""
        return self.template or self.task_type

    def fingerprint(self) -> str:
        """稳定指纹，用于留痕与"同一份特征选出同一组配置"的断言（`NFR-ST-03`）。"""
        payload = json.dumps(
            {
                "task_type": self.task_type,
                "risk": self.risk,
                "budget_tokens": self.budget_tokens,
                "budget_latency_ms": self.budget_latency_ms,
                "budget_cost": self.budget_cost,
                "environment": self.environment,
                "input_assets": sorted(self.input_assets),
                "template": self.template,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> str:
        parts = [f"类型={self.task_type or '未指定'}", f"风险={self.risk}"]
        if self.environment:
            parts.append(f"环境={self.environment}")
        if self.input_assets:
            parts.append("资产=" + ",".join(sorted(self.input_assets)))
        return "；".join(parts)


@dataclass(frozen=True)
class AgentProfile:
    """**一份配置**，不是实例（`FR-ST-15`）。

    七类组件各绑一个策略名 + 每个策略的版本 + 一组参数 + **选择依据**。

    **必须自解释**：拿到它要能回答"它是怎么被选出来的" —— 所以 ``layer``（走的哪一层）
    与 ``reason``（为什么）是它的必填部分，而不是可选注释。
    """

    name: str
    bindings: Mapping[ComponentKind, str] = field(default_factory=dict)
    #: ``组件.策略`` → 版本。**记了就要用**（`FR-ST-11`）
    versions: Mapping[str, str] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    layer: Layer = "default"
    reason: str = ""
    risk: RiskLevel = "low"

    @property
    def profile_key(self) -> str:
        """稳定的组合键。评测记录按它索引，**且必须与版本一起用**
        （版本对不上就视为没有数据，见 :class:`EvalRecord`）。"""
        parts = [f"{kind}={self.bindings.get(kind, '')}" for kind in COMPONENT_KINDS]
        return "|".join(parts)

    def strategy_of(self, kind: ComponentKind) -> str:
        return str(self.bindings.get(kind, ""))

    def version_of(self, kind: ComponentKind) -> str:
        return str(self.versions.get(f"{kind}.{self.bindings.get(kind, '')}", ""))

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "name": self.name,
            "bindings": {kind: self.bindings.get(kind, "") for kind in COMPONENT_KINDS},
            "versions": dict(self.versions),
            "params": dict(self.params),
            "layer": self.layer,
            "reason": self.reason,
            "risk": self.risk,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentProfile":
        """可序列化、可读回 —— `FR-ST-15` 的验收点就是"JSON 转一圈再读回来一致"。"""
        bindings = data.get("bindings") or {}
        return cls(
            name=str(data.get("name") or ""),
            bindings={kind: str(bindings.get(kind, "")) for kind in COMPONENT_KINDS},
            versions={str(k): str(v) for k, v in (data.get("versions") or {}).items()},
            params=dict(data.get("params") or {}),
            layer=str(data.get("layer") or "default"),  # type: ignore[arg-type]
            reason=str(data.get("reason") or ""),
            risk=str(data.get("risk") or "low"),  # type: ignore[arg-type]
        )

    def describe(self) -> str:
        """一行摘要：**带上它是怎么来的**，否则排障时看不出这次和上次差在哪。"""
        bound = " / ".join(
            f"{COMPONENT_LABELS.get(kind, kind)}={self.bindings.get(kind, '')}"
            for kind in COMPONENT_KINDS
            if self.bindings.get(kind)
        )
        return f"[{self.layer}] {self.name}：{bound or '（空）'}"


@dataclass(frozen=True)
class Violation:
    """一条**被违反的契约**（`FR-ST-06`）。

    ``components`` 与 ``detail`` 是必填的：`NFR-ST-06` 要求失败**指名道姓** ——
    只说"组合不合法"会让人对着七类组件挨个试。
    """

    rule: str
    components: tuple[ComponentKind, ...]
    detail: str
    suggestion: str = ""

    def __str__(self) -> str:
        names = " × ".join(COMPONENT_LABELS.get(c, c) for c in self.components)
        text = f"[{self.rule}] {names}：{self.detail}"
        if self.suggestion:
            text += f"\n  怎么办：{self.suggestion}"
        return text


@dataclass(frozen=True)
class Candidate:
    """一个候选组合与它的分数（`FR-ST-10` 要求分数可读）。"""

    profile: AgentProfile
    score: float = 0.0
    quality: float = 0.0
    cost: float = 0.0
    latency: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class SelectionDecision:
    """一次选择的结论。**四层 + 一个降级终点**，并且必须能解释为什么（`FR-ST-17`）。"""

    profile: AgentProfile | None = None
    layer: Layer = "default"
    reason: str = ""
    candidates: tuple[Candidate, ...] = ()
    violations: tuple[Violation, ...] = ()
    #: 降级到默认配置 —— **必须出声**（`FR-ST-12`）
    degraded: bool = False
    #: 高风险任务没有任何已验证组合 → **拒绝执行**，不是降级（`FR-ST-08`）
    refused: bool = False
    features: TaskFeatures | None = None

    @property
    def ok(self) -> bool:
        return self.profile is not None and not self.refused

    def __str__(self) -> str:
        if self.refused:
            return f"[拒绝] {self.reason}"
        mark = "降级" if self.degraded else self.layer
        return f"[{mark}] {self.profile.name if self.profile else '（无）'}：{self.reason}"


@dataclass(frozen=True)
class EvalRecord:
    """一条离线评测结论（`FR-ST-18`）。

    ``versions`` 是**版本向量**：选择器读它时逐项校验，只要有一个对不上，
    这条记录就**视为不存在**（`FR-ST-11`）——
    拿旧版结论套新实现是"**看起来有依据**"的决策，比没有依据更危险，
    因为它不会被怀疑。
    """

    profile_key: str
    #: ``组件.策略`` → 版本
    versions: Mapping[str, str] = field(default_factory=dict)
    quality: float = 0.0
    cost: float = 0.0
    latency: float = 0.0
    samples: int = 0
    benchmark_set: str = ""
    at: float = 0.0

    def matches(self, profile: AgentProfile) -> bool:
        """这条结论还适用吗 —— **逐个版本比对**。"""
        if self.profile_key != profile.profile_key:
            return False
        for key, version in self.versions.items():
            kind, _, name = key.partition(".")
            if profile.version_of(kind) != version:  # type: ignore[arg-type]
                return False
        # 反向也要比：profile 里有的策略，记录里必须都有版本 —— 否则
        # 一条"只记了两个组件"的旧记录会给一套七个组件的配置背书。
        return set(self.versions) == {
            f"{kind}.{name}"
            for kind in COMPONENT_KINDS
            if (name := profile.bindings.get(kind))
        }


def fingerprint_sequence(values: Sequence[str]) -> str:
    """一串名字的稳定指纹（枚举结果的"可重复"断言用它）。"""
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:16]


def parse_profile_key(key: str) -> Mapping[ComponentKind, str]:
    """把 :attr:`AgentProfile.profile_key` 还原成绑定表。

    **为什么需要它**：评测记录里只有 ``profile_key``（一张表不该重复存绑定表），
    而选择器要拿"这条记录说的是哪套配置"去和候选比对 ——
    所以键必须是**可逆**的。格式是 ``context=x|memory=y|...``，
    未知的组件名一律忽略（键可能来自更早的版本）。
    """
    bindings: dict[ComponentKind, str] = {}
    for part in key.split("|"):
        kind, _, name = part.partition("=")
        kind = kind.strip()
        if kind in COMPONENT_KINDS and name:
            bindings[kind] = name  # type: ignore[index]
    return bindings

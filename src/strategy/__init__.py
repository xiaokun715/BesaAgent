"""组件策略层：把「**这次任务该用哪套做法**」变成跑之前就能回答、且说得清理由的问题。

## 它做什么、不做什么（这一段比下面任何一段都重要）

**做四件事**：登记（`registry`）、校验（`contracts`）、选择（`selector`）、装配（`assembler`）。

**不实现任何策略**（`MST-1`）：

    一旦它开始"顺手实现一个"，上下文管理就会在 `context` 与本层各有一份 ——
    而那正是「gateway 的顺序规则曾有两个实现」那类问题的标准翻版。

各组件模块**声明**自己的策略，**组合根**持有工厂并真的构造实例，
而本层只认声明（架构文档 §6 的三分工）。

## 它守的两条线

1. **组合的错是沉默的错**（`docs/strategy` 需求 §2.2）：单条策略几乎不会错，
   错的是组合 —— 只读权限配需要写入的工具、任务状态压缩配结构化经验记忆……
   每一半都对，合起来任务整体失败，而**启动不报错、组件里也不报错**。
   所以契约校验发生在**装配之前**：那里的成本是一次纯函数调用，
   漏掉的成本是一轮真实调用 + 可能已发生的副作用 + 排查时间。
2. **安全是集合运算，不是目标函数的一项**（`MST-8`）：高风险任务只在
   「合法 ∩ 已验证」里选，没有就**拒绝执行**，而不是降级到未验证的配置。

## 依赖方向（硬约束）

本层**只依赖 `foundation`**，不 import 任何组件模块（`context` / `memory` /
`tool` / `agent` / `gateway`），也不 import `provider`（机械强制）。
这是它能被独立测试、且不会长成上帝模块的前提（`NFR-ST-01`）。
"""

from __future__ import annotations

from strategy.assembler import assemble, build_profile
from strategy.contracts import (
    Contract,
    combos_fingerprint,
    default_contracts,
    enumerate_valid,
    enumerate_valid_with_stats,
    validate,
)
from strategy.evaluator import BenchmarkTask, Evaluator, RunOutcomeLike, Runner
from strategy.registry import StrategyRegistry, UnknownStrategyError
from strategy.selector import (
    DEFAULT_EVENTS,
    EvaluatedSource,
    EventSink,
    NullEventSink,
    SelectionPolicy,
    StrategySelector,
)
from strategy.templates import (
    BASELINE_NAME,
    Template,
    build_templates,
    match_template,
    render_template,
)
from strategy.types import (
    COMPONENT_KINDS,
    COMPONENT_LABELS,
    SELECTION_LAYERS,
    AgentProfile,
    Candidate,
    ComponentKind,
    CostEstimate,
    EvalRecord,
    Layer,
    RiskLevel,
    SelectionDecision,
    StrategySpec,
    TaskFeatures,
    Violation,
    fingerprint_sequence,
    parse_profile_key,
)

__all__ = [
    "BASELINE_NAME",
    "COMPONENT_KINDS",
    "COMPONENT_LABELS",
    "DEFAULT_EVENTS",
    "SELECTION_LAYERS",
    "AgentProfile",
    "BenchmarkTask",
    "Candidate",
    "ComponentKind",
    "Contract",
    "CostEstimate",
    "EvalRecord",
    "EvaluatedSource",
    "Evaluator",
    "EventSink",
    "Layer",
    "NullEventSink",
    "RiskLevel",
    "RunOutcomeLike",
    "Runner",
    "SelectionDecision",
    "SelectionPolicy",
    "StrategyRegistry",
    "StrategySelector",
    "StrategySpec",
    "TaskFeatures",
    "Template",
    "UnknownStrategyError",
    "Violation",
    "assemble",
    "build_profile",
    "build_templates",
    "combos_fingerprint",
    "default_contracts",
    "enumerate_valid",
    "enumerate_valid_with_stats",
    "fingerprint_sequence",
    "match_template",
    "parse_profile_key",
    "render_template",
    "validate",
]

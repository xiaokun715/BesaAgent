"""``src/strategy`` 单测的公共夹具。

**全部用假策略声明**（`NFR-ST-04`）：这一层的每一条逻辑（注册、校验、选择、装配）
都**不该依赖任何真实组件** —— 它本来就不 import 它们（`NFR-ST-01`）。
假声明的另一个好处是**能把冲突构造出来**：真实注册表里凑不齐
"任务状态压缩 × 结构化经验记忆"这种组合，而假注册表可以。

词汇表（与 `composition/strategy.py` 的真实登记保持一致）：

======================  ==========================  ===============================
组件                     策略                        声明的输入 / 产出
======================  ==========================  ===============================
context                  ``full``                    provides: task_state
                         ``compressed``              （什么都不产出 ← 会丢字段的那个）
memory                   ``stateless``               （什么都不产出）
                         ``structured``              inputs: task_state
retrieval                ``plain``                   provides: evidence
                         ``case_kb``                 inputs: memory_content
tool_selection           ``fixed``                   （不需要写权限）
                         ``dynamic``                 inputs: write_access
permission               ``readonly``                （不提供写权限）
                         ``restricted``              provides: write_access
validation               ``format``                  provides: checked
                         ``cross``                   **不可用**（multiagent 未启用）
execution                ``single``                  （单次执行）
                         ``compare``                 inputs: multiple_candidates
======================  ==========================  ===============================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from foundation.clock import FakeClock

from strategy import (
    AgentProfile,
    CostEstimate,
    EvalRecord,
    SelectionPolicy,
    StrategyRegistry,
    StrategySelector,
    StrategySpec,
    Template,
    default_contracts,
)


def spec(
    kind: str,
    name: str,
    *,
    version: str = "1",
    provides: Sequence[str] = (),
    inputs: Sequence[str] = (),
    available: bool = True,
    reason: str = "",
    scenarios: Sequence[str] = (),
) -> StrategySpec:
    return StrategySpec(
        kind=kind,  # type: ignore[arg-type]
        name=name,
        version=version,
        interface=f"{kind.title()}Component",
        inputs=frozenset(inputs),
        provides=frozenset(provides),
        preconditions=(reason,) if reason else (),
        cost=CostEstimate(tokens=100, latency_ms=10),
        scenarios=tuple(scenarios) or (name,),
        metrics={"quality": "成功率"},
        available=available,
        unavailable_reason=reason,
    )


#: 假注册表：七个组件各两条（其中 validation 的第二条不可用）。
FAKE_SPECS: tuple[StrategySpec, ...] = (
    spec("context", "full", provides=("task_state",), scenarios=("多轮", "multi_turn")),
    spec("context", "compressed", scenarios=("长任务",)),
    spec("memory", "stateless", scenarios=("单次问答",)),
    spec("memory", "structured", inputs=("task_state",), provides=("memory_content",),
         scenarios=("故障复用",)),
    spec("retrieval", "plain", provides=("evidence",), scenarios=("技术问答",)),
    spec("retrieval", "case_kb", inputs=("memory_content",), provides=("evidence",),
         scenarios=("故障分析",)),
    spec("tool_selection", "fixed", scenarios=("单一操作",)),
    spec("tool_selection", "dynamic", inputs=("write_access",), scenarios=("复杂任务",)),
    spec("permission", "readonly", scenarios=("知识查询",)),
    spec("permission", "restricted", provides=("write_access",), scenarios=("环境操作",)),
    spec("validation", "format", provides=("checked",), scenarios=("用例生成",)),
    spec("validation", "cross", available=False, reason="multiagent 未启用",
         scenarios=("交叉验证",)),
    spec("execution", "single", scenarios=("简单任务",)),
    spec("execution", "compare", inputs=("multiple_candidates",), scenarios=("复杂诊断",)),
)


@pytest.fixture
def registry() -> StrategyRegistry:
    return StrategyRegistry(FAKE_SPECS)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_767_225_600)


def profile_of(registry: StrategyRegistry, **bindings: str) -> AgentProfile:
    """按绑定造一份 profile（缺的组件留空）。**测试里最常用的构造器。**"""
    from strategy.assembler import build_profile

    return build_profile(name="test", bindings=bindings, registry=registry)  # type: ignore[arg-type]


#: 一份合法的基线（每条规则都不违反）—— 大多数用例从它出发再改一处。
BASELINE_BINDINGS: Mapping[str, str] = {
    "context": "compressed",
    "memory": "stateless",
    "retrieval": "plain",
    "tool_selection": "fixed",
    "permission": "readonly",
    "validation": "format",
    "execution": "single",
}


@pytest.fixture
def baseline_template() -> Template:
    return Template(name="baseline", risk="low", bindings=dict(BASELINE_BINDINGS))


class FakeEvaluated:
    """假的评测数据源。**确定性** —— 选择的正确性不该依赖数据库里恰好有什么。"""

    def __init__(self, records: Sequence[EvalRecord] = ()) -> None:
        self._records = tuple(records)

    def records(self) -> Sequence[EvalRecord]:
        return self._records


def eval_record(
    profile: AgentProfile, *, quality: float = 0.8, cost: float = 100.0,
    latency: float = 50.0, samples: int = 30, at: float = 1.0, benchmark_set: str = "default",
) -> EvalRecord:
    return EvalRecord(
        profile_key=profile.profile_key,
        versions=dict(profile.versions),
        quality=quality, cost=cost, latency=latency,
        samples=samples, benchmark_set=benchmark_set, at=at,
    )


def make_selector(
    registry: StrategyRegistry,
    *,
    templates: Sequence[Template] = (),
    evaluated=None,
    events=None,
    clock=None,
    **policy_kwargs,
) -> StrategySelector:
    policy = SelectionPolicy(**policy_kwargs) if policy_kwargs else SelectionPolicy()
    return StrategySelector(
        registry=registry,
        contracts=default_contracts(registry),
        templates=templates,
        policy=policy,
        evaluated=evaluated,
        events=events,
        clock=clock,
    )

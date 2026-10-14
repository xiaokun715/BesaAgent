"""注册表与组件契约（`FR-ST-04..07`）。

**为什么这组值得写**：契约是这一层**唯一立刻见效**的部分（架构文档 §11）——
它不依赖任何评测数据、不需要任何组件改动，当天就能把四类"沉默的冲突"
挡在装配之前。而它失效的方式是**规则太宽**：什么都能通过，
于是问题又回到"运行时才发现"，且没有任何信号。
"""

from __future__ import annotations

import pytest

from strategy import (
    StrategyRegistry,
    StrategySpec,
    combos_fingerprint,
    default_contracts,
    enumerate_valid,
    enumerate_valid_with_stats,
    validate,
)
from strategy.registry import UnknownStrategyError
from tests.unit.strategy.conftest import BASELINE_BINDINGS, profile_of, spec


# ---------------------------------------------------------------- 注册与查找
def test_duplicate_registration_fails_hard(registry):
    """静默覆盖会让一条策略悄悄失效 —— 而表现是「某些任务突然换了做法」。"""
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(spec("context", "full", version="2"))


def test_same_name_in_different_kinds_is_allowed(registry):
    """``sliding_window`` 在上下文与记忆里都可能有 —— 所以键是二元组。"""
    registry.register(spec("memory", "full"))
    assert registry.has("context", "full") and registry.has("memory", "full")


def test_unknown_strategy_lists_the_whole_category(registry):
    """拼错是最常见的错误 —— 只说「未知」会让人去翻文档。"""
    with pytest.raises(UnknownStrategyError) as excinfo:
        registry.get("context", "nope")
    assert "full" in str(excinfo.value) and "compressed" in str(excinfo.value)


def test_unavailable_strategy_stays_registered(registry):
    """`FR-ST-05`：**保留在册 + 原因**，不是从表里消失。"""
    registry.mark_unavailable("validation", "format", "依赖未就绪")
    spec_ = registry.get("validation", "format")
    assert not spec_.available
    assert "依赖未就绪" in spec_.unavailable_reason
    assert "format" in [s.name for s in registry.by_kind("validation")]
    assert "format" not in [s.name for s in registry.available("validation")]


def test_mark_unavailable_requires_a_specific_reason(registry):
    """「不可用」三个字等于把问题留给下一个读代码的人。"""
    with pytest.raises(ValueError):
        registry.mark_unavailable("validation", "format", "  ")


def test_registry_reports_all_seven_components(registry):
    """`FR-ST-01` 的验收点：七个组件、每个下面的可用与不可用。"""
    described = registry.to_dict()
    assert len(described) == 7
    cross = [s for s in described["validation"] if s["name"] == "cross"][0]
    assert cross["available"] is False and "multiagent" in cross["reason"]


# ---------------------------------------------------------------- 契约：五条规则
def _violations(registry, profile):
    return validate(profile, default_contracts(registry))


def test_readonly_plus_write_tools_is_rejected(registry):
    """`CST-6`：说清「失败会表现为撞上步数上界」，而不是「不兼容」。"""
    profile = profile_of(
        registry, **{**BASELINE_BINDINGS, "tool_selection": "dynamic"}  # permission 仍是 readonly
    )
    violations = _violations(registry, profile)
    assert violations
    text = str(violations[0])
    assert violations[0].rule == "readonly_blocks_write_tools"
    assert "步数上界" in text, "错误信息要说**后果**，而不是三个字"
    assert set(violations[0].components) == {"permission", "tool_selection"}


def test_write_tools_with_write_permission_is_fine(registry):
    profile = profile_of(
        registry, **{**BASELINE_BINDINGS, "tool_selection": "dynamic", "permission": "restricted"}
    )
    assert _violations(registry, profile) == ()


def test_state_compression_loses_memory_fields(registry):
    """`CST-7`：压缩没保住记忆要的字段 → 存进去的是**残缺经验**。"""
    profile = profile_of(
        registry,
        **{**BASELINE_BINDINGS, "context": "compressed", "memory": "structured"},
    )
    violations = _violations(registry, profile)
    assert violations and violations[0].rule == "state_compression_loses_memory_fields"
    assert "残缺经验" in str(violations[0])


def test_full_context_satisfies_structured_memory(registry):
    profile = profile_of(
        registry, **{**BASELINE_BINDINGS, "context": "full", "memory": "structured"}
    )
    assert _violations(registry, profile) == ()


def test_unavailable_strategy_is_rejected_and_named(registry):
    """`CST-8` 前半：**指名道姓**地说清是哪个策略不可用、为什么。"""
    profile = profile_of(registry, **{**BASELINE_BINDINGS, "validation": "cross"})
    violations = _violations(registry, profile)
    assert violations
    assert "multiagent 未启用" in str(violations[0])
    assert violations[0].components == ("validation",)


def test_compare_with_single_execution_is_rejected(registry):
    """`CST-8` 后半：这套组合**没有意义**，而且它不会报错。"""
    profile = profile_of(registry, **{**BASELINE_BINDINGS, "execution": "compare"})
    violations = _violations(registry, profile)
    assert violations and violations[0].rule == "compare_needs_multiple_candidates"
    assert "没有意义" in str(violations[0])


def test_stateless_memory_conflicts_with_a_consumer(registry):
    """记忆选成「不记」而别的组件还要记忆内容 —— 同一套组合自相矛盾。"""
    profile = profile_of(
        registry,
        **{**BASELINE_BINDINGS, "memory": "stateless", "retrieval": "case_kb"},
    )
    violations = _violations(registry, profile)
    assert violations and violations[0].rule == "stateless_conflicts_with_structured_memory"
    assert "memory" in violations[0].components


def test_baseline_is_clean(registry):
    """基线必须每条规则都不违反 —— 它是降级目标（`FR-ST-16`）。"""
    assert _violations(registry, profile_of(registry, **BASELINE_BINDINGS)) == ()


def test_rules_are_open_for_extension(registry):
    """`FR-ST-06`：新增一条约束 = 加一个函数，**不改校验器本身**。"""
    def always_reject(profile):
        from strategy import Violation

        return (Violation(rule="my_rule", components=("context",), detail="测试用"),)

    profile = profile_of(registry, **BASELINE_BINDINGS)
    assert validate(profile, [*default_contracts(registry), always_reject]) != ()


# ---------------------------------------------------------------- 枚举
def test_enumeration_is_bounded_and_repeatable(registry):
    """`CST-9`：合法组合**可枚举**，且**可重复**。"""
    contracts = default_contracts(registry)
    first = enumerate_valid(registry, contracts)
    second = enumerate_valid(registry, contracts)
    assert first, "至少要有一些合法组合，否则这一层什么都不用做了"
    assert combos_fingerprint(first) == combos_fingerprint(second)


def test_enumeration_excludes_unavailable_strategies(registry):
    contracts = default_contracts(registry)
    valid = enumerate_valid(registry, contracts)
    assert valid
    for profile in valid:
        assert profile.strategy_of("validation") != "cross"


def test_adding_a_rule_lowers_the_count(registry):
    """`CST-10`：新增一条契约后数量下降，**且下降的那些能被解释**。"""
    from strategy import Violation

    contracts = default_contracts(registry)
    before = enumerate_valid(registry, contracts)

    def no_dynamic_tools(profile):
        if profile.strategy_of("tool_selection") == "dynamic":
            return (Violation(
                rule="no_dynamic_tools", components=("tool_selection",),
                detail="这一轮我们只想用固定工具集",
            ),)
        return ()

    after = enumerate_valid(registry, [*contracts, no_dynamic_tools])
    assert len(after) < len(before)
    assert all(p.strategy_of("tool_selection") != "dynamic" for p in after)


def test_stats_report_the_truth_about_total(registry):
    """`MST-12`：``total`` / ``valid`` 是**约束松紧的唯一观测指标** ——
    不能为了省事只检查前 N 个（那会让指标变成谎话）。"""
    contracts = default_contracts(registry)
    profiles, stats = enumerate_valid_with_stats(registry, contracts, limit=3)
    assert stats["total"] > stats["valid"] >= stats["returned"]
    assert stats["returned"] == 3 and stats["truncated"] == stats["valid"] - 3
    assert len(profiles) == 3


def test_enumeration_is_empty_when_a_kind_has_no_available_strategy():
    """某个组件下一条可用策略都没有 → 空集，而不是"配不出来"。"""
    registry = StrategyRegistry([
        spec("context", "only", available=False, reason="没实现"),
        *[s for s in __import__("tests.unit.strategy.conftest", fromlist=["FAKE_SPECS"]).FAKE_SPECS
          if s.kind != "context"],
    ])
    valid, stats = enumerate_valid_with_stats(registry, default_contracts(registry))
    assert valid == () and stats["empty_kinds"] == 1


def test_spec_requires_all_six_items():
    """`CST-1`：缺任何一项 → 硬失败，**并指出缺哪一项**。"""
    with pytest.raises(ValueError) as excinfo:
        StrategySpec(kind="context", name="x", version="1")
    message = str(excinfo.value)
    for missing in ("interface", "preconditions", "cost", "scenarios", "metrics"):
        assert missing in message


def test_empty_preconditions_means_filled_not_missing():
    """「无条件成立」请显式写 `()` —— 那是**填了**；不写才是缺。"""
    from strategy import CostEstimate

    ok = StrategySpec(
        kind="context", name="x", version="1", interface="C", preconditions=(),
        cost=CostEstimate(), scenarios=("a",), metrics={"q": "x"},
    )
    assert ok.preconditions == ()

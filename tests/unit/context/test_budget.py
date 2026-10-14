"""预算（`FR-C-12` / `FR-C-13`）。

**为什么这组值得写**：预算是**唯一一条不可协商的硬约束**（`NFR-C-02`）——
超了会被厂商拒。而它的失效方式很安静：算错不会报错，只会偶尔超窗，
或者白留一半预算。
"""

from __future__ import annotations

import pytest

from context.budget import BudgetPolicy, allocate_blocks
from context.types import ContextBlock, Provenance


def test_available_input_subtracts_output_and_margin():
    policy = BudgetPolicy(reserve_output_tokens=1000, safety_margin_tokens=200)
    plan = policy.plan(model_window=10_000, demands={})
    assert plan.available_input == 8_800


def test_system_and_current_come_first():
    """反过来的结果是「预算紧的时候先把任务本身砍了」。"""
    policy = BudgetPolicy(reserve_output_tokens=0, safety_margin_tokens=0)
    plan = policy.plan(model_window=1_000, demands={"system": 100, "current": 100, "window": 5_000})
    assert plan.used["system"] == 100
    assert plan.used["current"] == 100
    assert plan.used["window"] == 800   # 剩下的全给 window


def test_insufficient_when_even_fixed_does_not_fit():
    policy = BudgetPolicy(reserve_output_tokens=0, safety_margin_tokens=0)
    plan = policy.plan(model_window=100, demands={"system": 400, "current": 400})
    assert plan.insufficient
    # **不截断它们** —— 它们承载任务本身
    assert plan.used["system"] == 400 and plan.used["current"] == 400
    assert any("不会截断" in note for note in plan.notes)


def test_segments_split_by_ratio():
    policy = BudgetPolicy(
        reserve_output_tokens=0, safety_margin_tokens=0,
        segments={"window": 0.5, "transition": 0.2, "session_summary": 0.2, "long_term": 0.1},
    )
    plan = policy.plan(
        model_window=1_000,
        demands={"window": 900, "transition": 900, "session_summary": 900, "long_term": 900},
    )
    assert plan.allocated["window"] == 500
    assert plan.allocated["long_term"] == 100
    assert plan.dropped["window"] == 400


def test_segment_below_floor_is_dropped_whole():
    """低于下限就**整段不注入** —— 半截摘要比没有摘要更误导。"""
    policy = BudgetPolicy(
        reserve_output_tokens=0, safety_margin_tokens=0,
        segments={"window": 0.9, "transition": 0.03, "session_summary": 0.0, "long_term": 0.0},
        floors={"window": 10, "transition": 500, "session_summary": 10, "long_term": 10},
    )
    # window 要 800、transition 要 900，而总容量只有 1000 —— transition 只能分到 200，
    # 低于它的下限 500，于是**整段不注入**，而不是注入一个被切碎的过渡区。
    plan = policy.plan(model_window=1_000, demands={"window": 800, "transition": 900})
    assert plan.used["transition"] == 0
    assert plan.dropped["transition"] == 900
    assert any("整段不注入" in note for note in plan.notes)


def test_leftover_goes_to_higher_priority_first():
    """某个段没要满 → 剩的按优先级分给还没满足的段（window 优先）。"""
    policy = BudgetPolicy(
        reserve_output_tokens=0, safety_margin_tokens=0,
        segments={"window": 0.5, "transition": 0.2, "session_summary": 0.2, "long_term": 0.1},
    )
    plan = policy.plan(
        model_window=1_000,
        # window 要得少（300，刚好过了它 256 的下限）→ 它的余量会给 transition，
        # 而不是留给 long_term
        demands={"window": 300, "transition": 900, "session_summary": 0, "long_term": 900},
    )
    assert plan.used["window"] == 300
    assert plan.used["transition"] > 200          # 超过了它 0.2 的比例配额
    assert plan.used["transition"] > plan.used["long_term"]   # 优先级更高


def test_segments_sum_over_one_is_rejected_at_startup():
    """`C-C-6`：配置错误在**启动期**暴露，而不是让它悄悄超窗。"""
    with pytest.raises(ValueError, match="≤ 1.0"):
        BudgetPolicy.from_config(
            {"segments": {"window": 0.8, "transition": 0.5, "session_summary": 0, "long_term": 0}}
        )


def test_resolve_window_prefers_gateway_value():
    policy = BudgetPolicy(fallback_model_window=1_000)
    assert policy.resolve_window("a", from_gateway=8_000) == 8_000


def test_resolve_window_falls_back_and_says_so():
    """兜底可以，**悄悄兜底不行**。"""
    policy = BudgetPolicy(fallback_model_window=4_096)
    assert policy.resolve_window("a", from_gateway=None) == 4_096


def test_resolve_window_refuses_to_guess_when_no_fallback():
    policy = BudgetPolicy(fallback_model_window=None)
    assert policy.resolve_window("a", from_gateway=None) == 0


def test_allocate_blocks_keeps_whole_blocks():
    blocks = [ContextBlock(content="x" * 4 * 100, provenance=Provenance(segment="window")) for _ in range(3)]
    kept, dropped = allocate_blocks(blocks, quota=250)   # 每块 100 token
    assert len(kept) == 2 and len(dropped) == 1
    assert all(len(b.content) == 400 for b in kept)

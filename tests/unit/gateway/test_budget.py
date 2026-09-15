"""``CallBudget`` 与降级决策的单测。

**为什么单独一个文件**：``CallBudget`` 是 ``NFR-G-04``（故障不放大）的**唯一**落点，
它的语义一旦被改错，整个网关的可靠性保证就没了，而症状要到线上故障时才显现。
它是本模块最该被逐条钉死的东西。
"""

from __future__ import annotations

import math

import pytest

from foundation.clock import FakeClock
from gateway.fallback import FallbackPolicy, decide
from gateway.retry import CallBudget, RetryPolicy, compute_backoff, should_retry
from provider.errors import AuthError, UpstreamError


# --------------------------------------------------------------------------- #
# CallBudget
# --------------------------------------------------------------------------- #


def test_budget_counts_attempts():
    budget = CallBudget(max_attempts=3, clock=FakeClock())
    assert [budget.try_acquire() for _ in range(4)] == [True, True, True, False]
    assert budget.attempts_used == 3
    assert budget.exhausted_reason() == "attempts"


def test_budget_is_shared_across_candidates():
    """**跨候选累计** —— 这是「重试 × 候选」不会相乘的结构性保证。"""
    budget = CallBudget(max_attempts=4, clock=FakeClock())
    for _ in range(4):
        assert budget.try_acquire()
    # 换个候选再来：预算已经没了，换谁都不行
    assert budget.try_acquire() is False
    assert budget.exhausted_reason() == "attempts"


def test_budget_deadline_uses_monotonic_clock():
    """deadline 必须走 ``monotonic()``，否则系统调时会让它凭空超时或永不过期。"""
    clock = FakeClock()
    budget = CallBudget(max_attempts=10, clock=clock, deadline_s=5.0)

    assert budget.exhausted_reason() is None
    clock.advance(4.9)
    assert budget.exhausted_reason() is None
    clock.advance(0.2)
    assert budget.exhausted_reason() == "deadline"
    assert budget.try_acquire() is False
    # **attempts 还有剩余** —— 耗尽原因是 deadline 而不是次数。
    # 两者必须能区分：修法完全不同（调 total_max_attempts vs 调 deadline）。
    assert budget.remaining_attempts() == 10


def test_budget_without_deadline_never_expires_by_time():
    budget = CallBudget(max_attempts=5, clock=FakeClock())
    assert budget.remaining_s() == math.inf
    assert budget.exhausted_reason() is None


def test_has_room_for_rejects_waits_that_cannot_finish():
    """等不完的等待等于必然超时 —— 此时应当直接换候选，而不是白等。"""
    clock = FakeClock()
    budget = CallBudget(max_attempts=5, clock=clock, deadline_s=2.0)
    assert budget.has_room_for(1.0) is True
    assert budget.has_room_for(30.0) is False


# --------------------------------------------------------------------------- #
# RetryPolicy
# --------------------------------------------------------------------------- #


def test_retry_policy_rejects_budget_smaller_than_one_candidate():
    """总上限小于单候选上限 = 降级链永远走不到第二个候选。

    这条必须在**构造期**报错：等运行时才发现「备选从没被用过」，
    排查方向会跑偏成「备选模型有问题」。
    """
    with pytest.raises(ValueError) as excinfo:
        RetryPolicy(max_attempts_per_candidate=3, total_max_attempts=3)
    assert "降级链" in str(excinfo.value)


def test_retry_policy_from_config_defaults():
    policy = RetryPolicy.from_config(None)
    assert policy.total_max_attempts == 4
    assert policy.max_attempts_per_candidate == 2


def test_backoff_is_exponential():
    policy = RetryPolicy(backoff_base_s=1.0, jitter_ratio=0.0)
    assert compute_backoff(0, policy) == 1.0
    assert compute_backoff(1, policy) == 2.0
    assert compute_backoff(2, policy) == 4.0


def test_backoff_jitter_actually_varies():
    """抖动是必需的：同步退避会让多 agent 的重试同时到达，把刚恢复的上游再打挂。"""
    policy = RetryPolicy(backoff_base_s=1.0, jitter_ratio=0.5)
    samples = {round(compute_backoff(0, policy), 6) for _ in range(50)}
    assert len(samples) > 1


# --------------------------------------------------------------------------- #
# should_retry
# --------------------------------------------------------------------------- #


def test_should_retry_needs_both_conditions():
    budget = CallBudget(max_attempts=1, clock=FakeClock())
    retryable = UpstreamError("503")

    assert should_retry(retryable, budget) is True

    # 条件 2 单独失效：错误可重试，但预算耗尽
    budget.try_acquire()
    assert should_retry(retryable, budget) is False, "预算耗尽是独立的终止条件"

    # 条件 1 单独失效：预算充足，但错误不可重试
    fresh = CallBudget(max_attempts=10, clock=FakeClock())
    assert should_retry(AuthError("401"), fresh) is False


# --------------------------------------------------------------------------- #
# 降级决策
# --------------------------------------------------------------------------- #


def test_fallback_refuses_after_stream_committed():
    """**流式已输出正文后禁止降级**（FR-G-05）。

    用户已经看到半截答案，重发只会得到第二份不连贯的输出 ——
    那不是容错，是制造更糟的结果。
    """
    decision = decide(
        UpstreamError("boom"),
        policy=FallbackPolicy(),
        budget=CallBudget(max_attempts=10, clock=FakeClock()),
        remaining_candidates=3,
        stream_committed=True,
    )
    assert decision.proceed is False
    assert decision.reason == "stream_committed"


def test_fallback_refuses_when_budget_exhausted():
    budget = CallBudget(max_attempts=1, clock=FakeClock())
    budget.try_acquire()

    decision = decide(
        UpstreamError("boom"),
        policy=FallbackPolicy(),
        budget=budget,
        remaining_candidates=3,
    )
    assert decision.proceed is False
    assert decision.reason == "attempts"


def test_fallback_refuses_without_remaining_candidates():
    decision = decide(
        UpstreamError("boom"),
        policy=FallbackPolicy(),
        budget=CallBudget(max_attempts=10, clock=FakeClock()),
        remaining_candidates=0,
    )
    assert decision.proceed is False
    assert decision.reason == "no_more_candidates"


def test_fallback_proceeds_when_all_conditions_met():
    decision = decide(
        UpstreamError("boom"),
        policy=FallbackPolicy(),
        budget=CallBudget(max_attempts=10, clock=FakeClock()),
        remaining_candidates=1,
    )
    assert decision.proceed is True


def test_fallback_can_be_disabled():
    decision = decide(
        UpstreamError("boom"),
        policy=FallbackPolicy(enabled=False),
        budget=CallBudget(max_attempts=10, clock=FakeClock()),
        remaining_candidates=3,
    )
    assert decision.reason == "fallback_disabled"

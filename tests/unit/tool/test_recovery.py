"""异常分类与自愈（``tool/recovery.py``，`FR-T-17`）。

**最要紧的一条是「有副作用的工具不自动重试」** ——
自动重试一个 ``write`` 意味着可能写第二遍，而工具层**不知道**上一次到底执行了没有。
"""

from __future__ import annotations

import pytest

from tool.recovery import DEFAULT_REFUSE_AFTER, RetryBreaker, classify


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("参数不符合 read 的 schema：'path' is a required property", "param"),
        ("路径超出可读范围：/etc/passwd", "param"),
        ("命令超时（60s），已终止", "transient"),
        ("Connection reset by peer", "transient"),
        ("命令不存在：not found", "environment"),
        ("未授权：缺少 write 权限", "fatal"),
    ],
)
def test_classify_by_message(message: str, expected: str):
    """本模块的错误**有一半是我们自己拼的消息**（schema 不符、超范围、被拒绝），
    它们没有专属异常类型 —— 只用类型判会让这一半落到 ``unknown``。"""
    assert classify(message=message).kind == expected


def test_classify_by_exception_type_when_text_says_nothing():
    assert classify(FileNotFoundError("x")).kind == "environment"
    assert classify(PermissionError("x")).kind == "fatal"
    assert classify(TimeoutError()).kind == "transient"
    assert classify(ValueError("没说什么事")).kind == "param"


def test_unknown_stays_unknown():
    """认不出来就老实说认不出来 —— 别猜一个类别，那会让自愈做错事。"""
    assert classify(RuntimeError("某个奇怪的东西")).kind == "unknown"


# --------------------------------------------------------------------------- #
# 自愈动作 —— 关键在「敢不敢自动重试」
# --------------------------------------------------------------------------- #


def test_param_error_advises_with_a_hint():
    """参数错的正确动作是**给可操作的提示**，不是重试。

    「schema 不符」模型改不动；「第 3 个参数缺 ``path``」它能改。
    """
    recovery = classify(message="参数不符合 x 的 schema：缺少 path")
    assert recovery.action == "advise"
    assert recovery.hint
    assert not recovery.auto_retry


def test_transient_failure_on_a_read_tool_retries_once():
    recovery = classify(message="连接超时", side_effect="read")
    assert recovery.action == "retry_once"
    assert recovery.auto_retry


@pytest.mark.parametrize("effect", ["write", "destructive"])
def test_transient_failure_on_a_side_effecting_tool_does_not_auto_retry(effect: str):
    """**有副作用的不自动重试。**

    这是 `DT-16` 的核心：自动重试一个 ``write`` 意味着可能写第二遍 ——
    而工具层在「不确定」状态下**不知道**上一次到底执行了没有（`FR-T-08`）。
    要重试请上层显式决定。
    """
    recovery = classify(message="连接超时", side_effect=effect)
    assert recovery.action == "advise"
    assert not recovery.auto_retry, f"{effect} 类工具不得自动重试"
    assert "不会自动重试" in recovery.hint


def test_environment_error_suggests_switching():
    recovery = classify(message="命令不存在 not found")
    assert recovery.action == "switch"


def test_fatal_error_just_reports():
    recovery = classify(message="未授权")
    assert recovery.action == "report"
    assert not recovery.auto_retry


# --------------------------------------------------------------------------- #
# 无效重试熔断
# --------------------------------------------------------------------------- #


def test_breaker_refuses_after_the_threshold():
    """同一个工具 + 同一份参数连续失败 N 次之后开始拒绝。

    **这条独立于幂等**：模型很可能每次都生成一份**略微不同**的参数
    （多一个空格、换个路径写法）—— 键不同、幂等不生效，
    而它在做的是同一件注定失败的事。
    """
    breaker = RetryBreaker(refuse_after=3)
    assert not breaker.should_refuse("fp")

    for _ in range(3):
        breaker.record_failure("fp")

    assert breaker.should_refuse("fp")
    assert breaker.count("fp") == 3


def test_breaker_is_per_fingerprint():
    """不同的参数各自计数 —— 否则一个工具的失败会连累另一个。"""
    breaker = RetryBreaker(refuse_after=2)
    breaker.record_failure("a")
    breaker.record_failure("a")
    assert breaker.should_refuse("a")
    assert not breaker.should_refuse("b")


def test_breaker_clears_on_success():
    """成功之后**必须清零**。

    不清的话，一个工具在若干次偶发失败之后会被永久拉黑 ——
    而那看起来会像「这个工具坏了」。
    """
    breaker = RetryBreaker(refuse_after=2)
    breaker.record_failure("fp")
    breaker.record_failure("fp")
    assert breaker.should_refuse("fp")

    breaker.clear("fp")
    assert not breaker.should_refuse("fp")
    assert breaker.count("fp") == 0


def test_default_threshold_gives_the_model_two_chances():
    """默认给两次「改了但还是不对」的机会 —— 第三次说明它没在改对方向。"""
    assert DEFAULT_REFUSE_AFTER == 3


def test_describe_lists_only_the_blocked_fingerprints():
    """``describe()`` 给排障用 —— 只列已经被拉黑的，不要把所有计数都倒出来。"""
    breaker = RetryBreaker(refuse_after=2)
    breaker.record_failure("noise")
    breaker.record_failure("blocked")
    breaker.record_failure("blocked")

    described = breaker.describe()
    assert "blocked" in described
    assert "noise" not in described

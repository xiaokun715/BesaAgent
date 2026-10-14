"""模型的上下文窗口（`DC-1`）与 token 估算口径（`DC-2`）。

**为什么这两个值得单独测**：窗口是 ``src/context`` 分预算的**唯一依据**，
估算器是它算大小的**唯一尺子**。两者错了都不会报错 ——
只是偶尔超窗被厂商拒，或者白留一半预算。
"""

from __future__ import annotations

from gateway.registry import Registry
from gateway.tokens import CHARS_PER_TOKEN, estimate_messages_tokens, estimate_text_tokens


def _registry(models, aliases, *, clock=None) -> Registry:
    return Registry.from_config({"aliases": aliases, "models": models}, env={}, clock=clock)


def test_context_window_is_read_from_model_config(make_registry=None):
    registry = _registry(
        {
            "m-large": {"provider": "mock", "model": "x", "context_window": 128_000},
        },
        {"runtime.default": {"candidates": ["m-large"]}},
    )
    assert registry.context_window("runtime.default") == 128_000


def test_context_window_takes_the_smallest_candidate():
    """**取候选里最小的那个** —— 降级可能把请求交给链上任何一个候选，
    按最大的做预算，一旦降级就会**超窗被拒**（而超窗是厂商侧的错误，本地看不见原因）。
    """
    registry = _registry(
        {
            "m-big": {"provider": "mock", "model": "x", "context_window": 128_000},
            "m-small": {"provider": "mock", "model": "y", "context_window": 8_192},
        },
        {"runtime.default": {"candidates": ["m-big", "m-small"]}},
    )
    assert registry.context_window("runtime.default") == 8_192


def test_context_window_is_none_when_not_declared():
    """**不编一个默认值** —— 「缺省 = 多少」是使用方的决策，应当显式配置并出声。"""
    registry = _registry(
        {"m": {"provider": "mock", "model": "x"}},
        {"runtime.default": {"candidates": ["m"]}},
    )
    assert registry.context_window("runtime.default") is None


def test_context_window_is_on_the_model_spec():
    registry = _registry(
        {"m": {"provider": "mock", "model": "x", "context_window": 4096}},
        {"runtime.default": {"candidates": ["m"]}},
    )
    assert registry.model("m").context_window == 4096


def test_invalid_context_window_is_rejected_at_startup():
    """0 或负数会让预算算成负数 —— 表现成「所有上下文都被砍光」，离原因很远。"""
    import pytest

    with pytest.raises(ValueError, match="context_window"):
        _registry(
            {"m": {"provider": "mock", "model": "x", "context_window": 0}},
            {"runtime.default": {"candidates": ["m"]}},
        )


# ---------------------------------------------------------------- 估算口径
def test_estimate_text_tokens_is_empty_safe():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a") == 1
    assert estimate_text_tokens("a" * 400) == 400 // CHARS_PER_TOKEN


def test_estimate_messages_tokens_reads_content():
    class _M:
        def __init__(self, content):
            self.content = content

    total = estimate_messages_tokens([_M("a" * 40), _M("b" * 40)])
    assert total == 80 // CHARS_PER_TOKEN


def test_estimate_messages_tokens_is_at_least_one():
    assert estimate_messages_tokens([]) == 1

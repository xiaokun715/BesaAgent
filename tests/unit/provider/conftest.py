"""``src/provider`` 单测的公共夹具。

**全部测试都注入 ``httpx.MockTransport``，因此零真实网络请求**（``FR-P-13`` 验收点）。
这不是为了跑得快 —— 是为了让「上游返回 401 时该不该重试」这类断言**能稳定复现**。
真实网络下这些问题要么偶发，要么根本无法构造。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import httpx
import pytest

from foundation.clock import FakeClock
from provider import build_provider

#: 一个形状正确的最小 OpenAI 兼容响应
CHAT_BODY: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}

OPENAI_ENV = {"OPENAI_API_KEY": "sk-test1234567890abcdef"}

OPENAI_CFG: dict[str, Any] = {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "base_url": "https://api.openai.com/v1",
    "retries": 2,
    "backoff_s": 1.0,
    # 抖动用 0，让退避断言可预测；抖动本身另有专门的测试
    "backoff_jitter": 0.0,
}

ALL_CAPS = {
    "chat": True,
    "stream": True,
    "tools": True,
    "json": True,
    "vision": True,
    "embedding": True,
}


@pytest.fixture
def clock() -> FakeClock:
    """假时钟 —— 让「退避 2 秒」的测试不真的等 2 秒（NFR-G-07 同源需求）。"""
    return FakeClock()


@pytest.fixture
def make_openai() -> Callable[..., Any]:
    """用给定的 handler 装配一个 OpenAI provider。"""

    def _make(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        cfg: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: FakeClock | None = None,
    ):
        return build_provider(
            dict(OPENAI_CFG, **(cfg or {})),
            transport=transport or httpx.MockTransport(handler),
            env=OPENAI_ENV if env is None else env,
            clock=clock,
        )

    return _make


@pytest.fixture
def ok_handler():
    """永远成功。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CHAT_BODY)

    return _handler


class CountingHandler:
    """记录调用次数 —— 断言「是否重试」的唯一可靠办法。"""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self._responses = responses
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        item = self._responses[index]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def count(self) -> int:
        return len(self.calls)


class HangingTransport(httpx.AsyncBaseTransport):
    """永不返回的传输 —— 用来验证取消传播（``FR-P-14``）。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=CHAT_BODY)  # pragma: no cover

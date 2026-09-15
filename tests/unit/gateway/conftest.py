"""``src/gateway`` 单测的公共夹具。

全部经由 ``httpx.MockTransport`` 注入，**零真实网络请求**。
网关的多数断言（「该不该重试」「熔断后有没有真的跳过」）依赖**精确的调用次数**，
而真实网络下这些数字根本不可复现。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx
import pytest

from foundation.clock import FakeClock
from gateway.cost import CostSheet, Price
from gateway.gateway import Gateway, NullEmitter
from gateway.rate_limit import LocalRateLimiter, RateLimitPolicy
from gateway.registry import Registry

CHAT_BODY: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
}

OPENAI_ENV = {"OPENAI_API_KEY": "sk-gateway-test1234567890"}


def chat_body(content: str = "你好", **usage: int) -> dict[str, Any]:
    body = {
        "model": "m",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }
    if usage:
        body["usage"] = usage
    return body


def model_spec(host: str, *, priority: int = 0, name: str = "m", **extra: Any) -> dict[str, Any]:
    """一个最小可用的模型配置。``host`` 决定路由到哪个假端点。"""
    return {
        "provider": "openai",
        "model": name,
        "base_url": f"https://{host}/v1",
        "priority": priority,
        **extra,
    }


class HostRouter:
    """按 host 分发的假端点，并记录**每一次**调用。

    「上游被打了几次」是网关多数验收点的核心断言（B-6 / B-7 / B-9 / B-10），
    所以调用记录必须精确到次，且能按 host 分组。
    """

    def __init__(self) -> None:
        self._routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.calls: list[str] = []

    def on(self, host: str, responder: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> HostRouter:
        self._routes[host] = responder if callable(responder) else (lambda _req: responder)
        return self

    def count(self, host: str) -> int:
        return self.calls.count(host)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        responder = self._routes.get(host)
        if responder is None:
            raise AssertionError(f"未配置的假端点：{host}")
        return responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gateway_factory(clock: FakeClock) -> Callable[..., Gateway]:
    """装配一个可用的 Gateway。"""

    def _make(
        *,
        models: Mapping[str, Any],
        aliases: Mapping[str, Any],
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        env: Mapping[str, str] | None = None,
        prices: Mapping[str, Price] | None = None,
        currency: str = "CNY",
        **gateway_kwargs: Any,
    ) -> Gateway:
        registry = Registry.from_config(
            {"aliases": aliases, "models": models},
            env=OPENAI_ENV if env is None else env,
            clock=clock,
            provider_options={"transport": transport or httpx.MockTransport(handler or (lambda r: httpx.Response(200, json=CHAT_BODY)))},
        )
        gateway_kwargs.setdefault("clock", clock)
        gateway_kwargs.setdefault("cost", CostSheet(prices or {}, currency=currency))
        gateway_kwargs.setdefault("deadline_s", 60.0)
        return Gateway(registry, **gateway_kwargs)

    return _make


class RecordingEmitter(NullEmitter):
    """记录事件序列 —— 验证「重试→降级→成功」的完整链路（B-14）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:
        self.events.append((name, dict(payload)))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == name]


class BlockingTransport(httpx.AsyncBaseTransport):
    """挂起直到被取消 —— 用于验证取消路径上的配额归还（B-15）。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=CHAT_BODY)  # pragma: no cover


class BrokenStreamTransport(httpx.AsyncBaseTransport):
    """先吐出若干分片再断开 —— 用于验证「流式已输出后禁止降级」（B-10）。

    **自带调用计数**：这类测试的传输层是自定义的（不是 ``HostRouter``），
    而「一共发起了几次上游调用」恰恰是核心断言 —— 没有计数就只能靠推测。
    """

    def __init__(self, pieces: int = 1) -> None:
        self.pieces = pieces
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.host)

        async def body():
            for index in range(self.pieces):
                payload = json.dumps(
                    {"choices": [{"delta": {"content": f"片{index}"}}]}, ensure_ascii=False
                )
                yield f"data: {payload}\n\n".encode("utf-8")
            raise httpx.ReadError("connection lost mid-stream")

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_AsyncByteStream(body()),
        )


class _AsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, generator) -> None:
        self._generator = generator

    async def __aiter__(self):
        async for chunk in self._generator:
            yield chunk

    async def aclose(self) -> None:
        return None


def limiter(clock: FakeClock, **policy: Any) -> LocalRateLimiter:
    return LocalRateLimiter(RateLimitPolicy(**policy), clock=clock, on_exceed="wait")


__all__ = [
    "BrokenStreamTransport",
    "BlockingTransport",
    "CHAT_BODY",
    "HostRouter",
    "OPENAI_ENV",
    "RecordingEmitter",
    "chat_body",
    "limiter",
    "model_spec",
]

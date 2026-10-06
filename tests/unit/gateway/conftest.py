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

    **同时记录请求体**（:attr:`bodies`）—— 没有它就断言不了「参数有没有真的透传到上游」。
    补这一项之前，把 ``chat()`` 里的 ``temperature=temperature`` 改成写死的
    ``0.0``，**400 多条测试全绿**：因为夹具只看 host，压根看不到发出去的内容。
    这类「网关只是把你给的东西转手递下去」的路径，只有看请求体才测得出来。
    """

    def __init__(self) -> None:
        self._routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.calls: list[str] = []
        #: 每次调用收到的请求体（已解析成 dict）。**与 :attr:`calls` 同序**。
        #: 非 JSON 的请求体会记成 ``{}`` —— 那说明这条断言本身写错了地方。
        self.bodies: list[dict[str, Any]] = []

    def on(self, host: str, responder: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> HostRouter:
        self._routes[host] = responder if callable(responder) else (lambda _req: responder)
        return self

    def count(self, host: str) -> int:
        return self.calls.count(host)

    def body(self, index: int = -1) -> dict[str, Any]:
        """取第 ``index`` 次调用的请求体（默认最后一次）。"""
        return self.bodies[index]

    def body_to(self, host: str) -> dict[str, Any]:
        """取**发往该 host** 的最后一次请求体。

        比按下标取稳：重试或降级会让调用次数变化，而下标随之漂移 ——
        断言会跟着变成「测的是第几次」而不是「测的是发了什么」。
        """
        for seen, body in zip(reversed(self.calls), reversed(self.bodies)):
            if seen == host:
                return body
        raise AssertionError(f"没有发往 {host!r} 的调用；实际发往：{self.calls}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        self.bodies.append(_decode_body(request))
        responder = self._routes.get(host)
        if responder is None:
            raise AssertionError(f"未配置的假端点：{host}")
        return responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _decode_body(request: httpx.Request) -> dict[str, Any]:
    try:
        data = json.loads(request.content)
    except Exception:  # noqa: BLE001 - 夹具不因解析失败而中断，让断言去暴露
        return {}
    return data if isinstance(data, dict) else {}


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


def embed_body(count: int = 1, dim: int = 8, model: str = "m") -> dict[str, Any]:
    """一个形状正确的 embedding 响应。

    ``index`` **必须齐且是 0..n-1** —— provider 在批量 ≥9 条时对上游的分片重置
    有专门的防御（见 ``openai/embedding.py``），这里给一个规规矩矩的。
    """
    return {
        "model": model,
        "data": [
            {"index": i, "embedding": [float(i) / 10] * dim} for i in range(count)
        ],
        "usage": {"prompt_tokens": 7 * count},
    }


class StreamingTransport(httpx.AsyncBaseTransport):
    """**正常结束**的 SSE 流：吐出若干分片后自然收尾。

    与 :class:`BrokenStreamTransport` 形成对照 —— 那个测的是「断开了怎么办」，
    这个测的是「没断开时应该拿到什么」。只有后者覆盖了流式的**基线路径**。
    """

    def __init__(self, pieces: int = 3, *, usage: bool = True) -> None:
        self.pieces = pieces
        self.usage = usage
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.host)
        pieces = self.pieces
        usage = self.usage

        async def body():
            for index in range(pieces):
                payload = json.dumps(
                    {"model": "m", "choices": [{"delta": {"content": f"片{index}"}}]},
                    ensure_ascii=False,
                )
                yield f"data: {payload}\n\n".encode("utf-8")
            if usage:
                # OpenAI 兼容协议里用量是**最后一个 chunk**，且 choices 为空。
                # 注意 gateway 的流式路径拿不到它（契约是 AsyncIterator[str]），
                # 这里给出来是为了让上游形状真实 —— 别据此以为流式有成本。
                final = json.dumps(
                    {"model": "m", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 9}}
                )
                yield f"data: {final}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_AsyncByteStream(body()),
        )


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
    "StreamingTransport",
    "chat_body",
    "embed_body",
    "limiter",
    "model_spec",
]

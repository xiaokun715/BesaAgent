"""OpenAI 兼容协议的 HTTP 传输。

**这个类是 OpenAI 兼容形状的基准实现**，``dashscope/`` 与 ``vllm/`` 通过继承复用它，
只覆写差异部分（见 ``provider/openai/__init__.py`` 的说明）。

**职责边界**（架构概要设计-provider §3）：

- **只做**：连接池、超时、认证头、SSE 拆行、**网络层**重试、错误归一化。
- **不做**：请求体长什么样、响应字段怎么读 —— 那是 ``llm.py`` 的事。

**重试边界（D-1 的落地）**：只重试**连接失败与读写超时**，即「请求大概率没到达模型」
的那一类。HTTP 状态码错误（含 5xx / 429）**一律上抛** ——
厂商故障需要**跨模型**决策，换一个模型往往比锤同一个更有用；
而且 provider 的重试会与 gateway 的重试**相乘**，这是重试风暴的主要来源。
"""

from __future__ import annotations

import logging
import random
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from foundation.clock import Clock, SystemClock
from provider.base import Client
from provider.errors import (
    NetworkError,
    ProtocolError,
    TimeoutError,
    map_http_status,
    parse_retry_after,
)

__all__ = ["HttpClient"]

_log = logging.getLogger(__name__)

#: 连不上 / 连接被拒 / 池耗尽 —— 请求没发出去
_NETWORK_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)
#: 发出去但读不回来 —— 归 TimeoutError，语义上更接近「上游慢」而非「网络不通」
_TIMEOUT_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
)


class HttpClient(Client):
    """OpenAI 兼容端点的传输客户端。"""

    provider_name: str = "openai"

    #: 是否发送 ``Authorization`` 头。**本地端点置 False**（FR-P-12）——
    #: 发一个空的 ``Bearer `` 会被部分网关判为「鉴权失败」，而不是「无需鉴权」。
    requires_auth: bool = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout_s: float = 60.0,
        retries: int = 1,
        backoff_s: float = 1.5,
        backoff_jitter: float = 0.3,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        """
        Args:
            transport: **仅供测试注入**（``httpx.MockTransport``）——
                这是让单测全程零真实网络请求的唯一入口（FR-P-13）。
            clock: 可注入时钟，测试用 ``FakeClock`` 秒过退避（NFR-G-07 同源需求）。
        """
        if not base_url:
            raise ValueError(f"{type(self).__name__} 缺少 base_url")

        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._default_timeout = float(timeout_s)
        self._retries = max(0, int(retries))
        self._backoff_s = float(backoff_s)
        self._jitter = float(backoff_jitter)
        self._clock: Clock = clock or SystemClock()
        self._closed = False

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._default_timeout,
            transport=transport,
            headers=self._headers(),
        )

    # ---------------------------------------------------------------- 配置
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.requires_auth and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _timeout(self, override: float | None) -> float:
        return self._default_timeout if override is None else float(override)

    def _error_context(self, trace_id: str) -> dict[str, Any]:
        return {"provider": self.provider_name, "model": self.model, "trace_id": trace_id}

    def _backoff_delay(self, attempt: int) -> float:
        """指数退避 + **抖动**。

        抖动不是可选项：多 agent 并发时，同步退避会让所有重试在同一时刻到达，
        把刚恢复的上游**再打挂一次**。抖动把这些请求摊开。
        """
        base = self._backoff_s * (2**attempt)
        if self._jitter > 0:
            base *= 1.0 + random.uniform(-self._jitter, self._jitter)
        return max(0.0, base)

    # ---------------------------------------------------------------- 非流式
    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        timeout = self._timeout(timeout_s)
        ctx = self._error_context(trace_id)
        attempt = 0

        while True:
            last: Exception | None = None
            try:
                response = await self._client.post(path, json=dict(payload), timeout=timeout)
            except _TIMEOUT_ERRORS as exc:
                last = TimeoutError(
                    f"上游读超时：{type(exc).__name__}", **ctx
                )
            except _NETWORK_ERRORS as exc:
                last = NetworkError(
                    f"网络错误：{type(exc).__name__}: {exc}", **ctx
                )
            except httpx.HTTPError as exc:
                # 兜底：httpx 新增的异常类型。**不重试** —— 不认识的异常重试是赌博。
                raise NetworkError(f"HTTP 传输失败：{type(exc).__name__}: {exc}", **ctx) from exc
            else:
                # 状态码错误**不上抛之前先不重试** —— 见模块 docstring 的重试边界。
                if response.status_code >= 400:
                    raise map_http_status(
                        response.status_code,
                        body=response.text,
                        retry_after=parse_retry_after(response.headers),
                        **ctx,
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProtocolError(
                        f"响应非 JSON（HTTP {response.status_code}）：{response.text[:200]}", **ctx
                    ) from exc

            if attempt >= self._retries:
                raise last
            delay = self._backoff_delay(attempt)
            _log.warning(
                "传输失败，%.2fs 后重试（第 %d/%d 次）：%s trace=%s",
                delay, attempt + 1, self._retries, last, trace_id,
            )
            await self._clock.sleep(delay)
            attempt += 1

    # ---------------------------------------------------------------- 流式
    def stream_sse(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        trace_id: str = "",
    ) -> AsyncIterator[str]:
        """逐行产出 SSE 行。

        **刻意不是 ``async def``**（``base.Client.stream_sse`` 的说明）：
        这里只做参数准备就返回迭代器，真正的连接等首次迭代才建立。
        「请求了不支持的模型」这类**校验**错误由 ``llm.stream_chat`` 在调用瞬间抛 ——
        校验与建连分离，让「配置错了」和「网络挂了」是两类可区分的失败。
        """
        return self._stream_impl(
            path, dict(payload), self._timeout(timeout_s), trace_id
        )

    async def _stream_impl(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float,
        trace_id: str,
    ) -> AsyncIterator[str]:
        ctx = self._error_context(trace_id)
        # 整个流式过程都要包在归一化里，**不只是建连那一下**。
        # 长连接在传输中途断开（ReadError / RemoteProtocolError）是最常见的流式故障，
        # 而它发生在 `async for` 内部 —— 漏掉这一层，上游就会收到一个原始的
        # httpx 异常，既没有 retryable 语义，也带着传输层的内部细节。
        try:
            async with self._client.stream(
                "POST", path, json=payload, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    raise map_http_status(
                        response.status_code,
                        body=raw,
                        retry_after=parse_retry_after(response.headers),
                        **ctx,
                    )
                async for line in response.aiter_lines():
                    # 空行是 SSE 的分隔符，不是数据；丢掉以免下游反复做同样的判断
                    if line.strip():
                        yield line
        except _TIMEOUT_ERRORS as exc:
            raise TimeoutError(f"流式读超时：{type(exc).__name__}", **ctx) from exc
        except _NETWORK_ERRORS as exc:
            raise NetworkError(f"流式传输中断：{type(exc).__name__}: {exc}", **ctx) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"流式传输失败：{type(exc).__name__}: {exc}", **ctx) from exc

    # ---------------------------------------------------------------- 生命周期
    async def aclose(self) -> None:
        """**幂等** —— ``container`` 关停时可能重复调用。"""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    def __repr__(self) -> str:
        # 不输出 _api_key（NFR-P-04）
        return f"{type(self).__name__}(base_url={self.base_url!r}, model={self.model!r})"

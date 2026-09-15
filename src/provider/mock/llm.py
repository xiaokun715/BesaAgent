"""确定性假对话模型。

**它不是「测试用的桩」，它是契约的一部分**（``FR-P-15``）：
「无密钥环境能跑通全链路」是 CI 的硬前提，而前提必须是**产品代码的一部分** ——
放在 tests 里的后果是每个测试文件各写一个假实现，然后行为渐渐不一致，
最后 CI 跑的是几个互不相同的假契约。

**两个刻意做出来的性质**：

1. **响应可编排**。``MockChatScript`` 是一个按序弹出的响应列表，
   元素可以是 :class:`ChatResponse`（成功）或 :class:`ProviderError` 子类实例（失败）。
   这让「第一次超时、第二次成功」这类路径可测 —— 而那正是 gateway 重试逻辑的主战场。

2. **校验在调用瞬间执行**。``stream_chat`` 是普通 ``def``，且**在返回迭代器之前**
   就做完校验与错误抛出。真实适配器要满足 ``FR-P-08``（本地拦截）也必须这样 ——
   假实现在这里做对了，真实现才有参照。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from provider.base import ChatModel, guard_request, validate_messages
from provider.types import (
    Capability,
    ChatRequest,
    ChatResponse,
    Usage,
)

__all__ = ["MockChatModel", "MockChatScript"]

_log = logging.getLogger(__name__)

DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.CHAT, Capability.STREAM, Capability.TOOLS, Capability.JSON, Capability.VISION}
)


class MockChatScript:
    """按序弹出的响应编排。

    用法::

        script = MockChatScript([
            ProviderTimeout("上游超时"),          # 第一次：可重试失败
            ChatResponse(content="好了", model="mock"),   # 第二次：成功
        ])

    **列表耗尽后重复最后一项**，而不是报错 ——
    后者会让「批量调用同一个假模型」的测试被迫准备 N 份响应，
    而它其实只关心第 N 次之后的行为是否稳定。
    """

    def __init__(
        self,
        responses: Sequence[Any] | None = None,
        *,
        default: Any = None,
        stream_chunks: Sequence[str] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._default = default
        self._stream_chunks = list(stream_chunks or ["这是", "一段", "流式", "输出"])
        self._cursor = 0

    def next_response(self, req: ChatRequest) -> Any:
        """弹出下一个响应；耗尽后重复最后一项（或 ``default``）。"""
        if self._cursor < len(self._responses):
            item = self._responses[self._cursor]
            self._cursor += 1
            return item
        if self._responses:
            return self._responses[-1]
        if self._default is not None:
            return self._default
        return ChatResponse(
            content=f"[mock] 收到 {len(req.messages)} 条消息",
            model="mock",
            finish_reason="stop",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    @property
    def chunks(self) -> list[str]:
        return list(self._stream_chunks)

    @property
    def call_count(self) -> int:
        """已被消费的次数 —— 断言「重试了几次」时用它。"""
        return self._cursor


class MockChatModel(ChatModel):
    """确定性假对话模型。不发起任何网络请求。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        return DEFAULT_CAPABILITIES

    def __init__(self, cfg: Any, client: Any, *, script: MockChatScript | None = None) -> None:
        super().__init__(cfg, client)
        self.script = script or MockChatScript()
        #: 收到的全部请求（含被拒的）。断言「payload 里到底发了什么」时用它 ——
        #: 比在真实适配器上打桩可靠得多。
        self.requests: list[ChatRequest] = []

    # ------------------------------------------------------------------ 契约
    async def chat(self, req: ChatRequest) -> ChatResponse:
        validate_messages(req.messages, model=self.model_name)
        guard_request(
            req, self.capabilities(), model=self.model_name, provider=self.provider_name
        )
        self.requests.append(req)

        item = self.script.next_response(req)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ChatResponse):
            return item
        return ChatResponse(content=str(item), model=self.model_name, finish_reason="stop")

    def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        """校验与错误**在调用瞬间**抛出，而不是等首次迭代（``FR-P-08`` 的参照实现）。"""
        validate_messages(req.messages, model=self.model_name)
        guard_request(
            req, self.capabilities(), model=self.model_name, provider=self.provider_name
        )
        self.requests.append(req)

        item = self.script.next_response(req)
        if isinstance(item, BaseException):
            # 关键：这里抛，不是在被返回的生成器里抛。
            # 真实适配器的 stream_chat 也必须这样，否则错误位置会远离调用点。
            raise item
        if isinstance(item, ChatResponse) and item.content:
            return self._yield_text(item.content)

        return self._stream_chunks()

    # ------------------------------------------------------------------ 内部
    async def _yield_text(self, text: str) -> AsyncIterator[str]:
        yield text

    async def _stream_chunks(self) -> AsyncIterator[str]:
        for chunk in self.script.chunks:
            yield chunk

    async def aclose(self) -> None:
        """mock 没有连接要释放，但保持幂等以匹配真实实现的调用方预期。"""
        return None

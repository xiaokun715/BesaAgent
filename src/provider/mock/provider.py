"""Mock 厂商描述。

**没有 ``client.py``** —— 假实现不发网络请求，也就不需要传输层。
``Client`` 是抽象类，所以这里用 :class:`NullClient` 占位：
它对任何调用都抛出「不该被调用」的错误，而不是静默返回空值。

这个占位本身是有价值的：如果哪天有人在 mock 路径上意外走到了传输层，
``NullClient`` 会**立刻**报出来，而不是返回一个空 dict 让错误往下游飘。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from provider.base import ChatModel, Client, EmbeddingModel, Provider
from provider.mock.embedding import MockEmbeddingModel
from provider.mock.llm import MockChatModel, MockChatScript
from provider.types import Capability

__all__ = ["MockProvider", "NullClient"]


class NullClient(Client):
    """占位传输。任何调用都抛错 —— 见模块 docstring。"""

    provider_name = "mock"

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        trace_id: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "mock provider 不应发起网络请求；走到这里说明装配接错了"
        )

    def stream_sse(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout_s: float | None = None,
        trace_id: str = "",
    ) -> AsyncIterator[str]:
        raise NotImplementedError(
            "mock provider 不应发起网络请求；走到这里说明装配接错了"
        )

    async def aclose(self) -> None:
        return None


class MockProvider(Provider):
    """确定性假厂商。CI 与本地开发默认走这条。"""

    NAME = "mock"
    DEFAULT_BASE_URL = "mock://local"
    API_KEY_ENV = ""
    #: 无密钥照常工作 —— 这是 CI 能在无 .env 环境跑通的前提（NFR-P-05）
    REQUIRES_API_KEY = False

    DEFAULT_CAPABILITIES = frozenset(
        {
            Capability.CHAT,
            Capability.STREAM,
            Capability.TOOLS,
            Capability.JSON,
            Capability.VISION,
            Capability.EMBEDDING,
        }
    )

    def __init__(
        self,
        cfg: Any,
        *,
        script: MockChatScript | None = None,
        transport: Any | None = None,
        env: Mapping[str, str] | None = None,
        clock: Any | None = None,
    ) -> None:
        self._script = script or MockChatScript()
        super().__init__(cfg, transport=transport, env=env, clock=clock)

    def _make_client(self) -> Client:
        return NullClient()

    def chat_model(self) -> ChatModel:
        return MockChatModel(self.config, self.client(), script=self._script)

    def embedding_model(self) -> EmbeddingModel:
        return MockEmbeddingModel(self.config, self.client())

"""DashScope（阿里百炼）厂商描述。

**走 OpenAI 兼容模式**（``/compatible-mode/v1``），这是《架构概要设计-provider》§9 D-B
的落地：原生协议相对兼容模式没有本项目需要的能力，却会引入厂商 SDK 依赖和一整套
需要单独维护的字段映射。

端点由 ``DEFAULT_BASE_URL`` 提供，但**配置里显式写 ``base_url`` 会覆盖它** ——
内网代理、私有部署、灰度环境都靠这条。
"""

from __future__ import annotations

from provider.base import ChatModel, Client, EmbeddingModel, Provider
from provider.dashscope.client import DashScopeClient
from provider.dashscope.embedding import DashScopeEmbeddingModel
from provider.dashscope.llm import DashScopeChatModel
from provider.types import Capability

__all__ = ["DashScopeProvider"]


class DashScopeProvider(Provider):
    """DashScope 兼容模式。"""

    NAME = "dashscope"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    API_KEY_ENV = "DASHSCOPE_API_KEY"
    REQUIRES_API_KEY = True

    #: 与 OpenAI 同构，且同样**不含 ``VISION`` 与 ``EMBEDDING``** ——
    #: 两者都是「模型的事实」而非「厂商的事实」：``qwen-vl`` 才有视觉，
    #: ``text-embedding-v3`` 才做向量化，而它们共用一个 dashscope 适配器。
    #: 放进默认集会让对话请求可能被路由到纯向量模型（或反过来），
    #: 然后在上游得到一个指不到配置的 400。由配置显式声明（FR-P-08）。
    DEFAULT_CAPABILITIES = frozenset(
        {
            Capability.CHAT,
            Capability.STREAM,
            Capability.TOOLS,
            Capability.JSON,
        }
    )

    def _make_client(self) -> Client:
        cfg = self.config
        return DashScopeClient(
            base_url=cfg.base_url,
            api_key=self._resolve_api_key(),
            model=cfg.model,
            timeout_s=cfg.timeout_s,
            retries=cfg.retries,
            backoff_s=cfg.backoff_s,
            backoff_jitter=cfg.backoff_jitter,
            transport=self._transport,
            clock=self._clock,
        )

    def chat_model(self) -> ChatModel:
        return DashScopeChatModel(self.config, self.client())

    def embedding_model(self) -> EmbeddingModel:
        return DashScopeEmbeddingModel(self.config, self.client())

"""vLLM 厂商描述。

**没有 ``embedding.py``，且这不是遗漏**（架构概要设计-provider §3.3）：
vLLM 部署的通常是 chat 模型，向量化走不到它。

这一条直接推导出契约上的一个要求：``embedding_model()`` 必须能表达「本厂商不支持」。
本类**不覆写**它，因此继承 ``Provider.embedding_model()`` 的默认行为 ——
抛 :class:`CapabilityNotSupportedError`。走到那里说明上层的**能力过滤失效了**，
那是编程错误，必须立刻暴露，而不是返回 ``None`` 让调用方在错误的地方做检查。
"""

from __future__ import annotations

from provider.base import ChatModel, Client, Provider
from provider.types import Capability
from provider.vllm.client import VLLMClient
from provider.vllm.llm import VLLMChatModel

__all__ = ["VLLMProvider"]


class VLLMProvider(Provider):
    """自建 vLLM。"""

    NAME = "vllm"

    #: **刻意留空**：自建服务没有公认地址，必须由配置提供
    #: （``base_url``，或声明 ``provider:`` 由 ``providers:`` 段给出）。
    #: 留空让缺失变成**构造期**的明确报错，而不是运行时连到某个错误的默认地址。
    DEFAULT_BASE_URL = ""

    API_KEY_ENV = "VLLM_API_KEY"
    #: 本地端点：无密钥照常工作（FR-P-12 / 验收场景 A-3）
    REQUIRES_API_KEY = False

    #: 最小集。实际能力由部署决定，配置里按需打开（FR-P-08）——
    #: 详见 ``vllm/llm.py`` 的说明。
    DEFAULT_CAPABILITIES = frozenset({Capability.CHAT, Capability.STREAM})

    def _make_client(self) -> Client:
        cfg = self.config
        return VLLMClient(
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
        return VLLMChatModel(self.config, self.client())

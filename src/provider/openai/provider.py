"""OpenAI 厂商描述。

**本模块刻意不 import ``httpx``**，也不做任何 I/O —— 它只是一组静态事实加上一个装配入口。
这条性质让「能力矩阵」「默认端点」「凭据约定」可以在**无网络、无密钥**的环境里被单测直接断言
（架构概要设计-provider §1.2）。
"""

from __future__ import annotations

from provider.base import ChatModel, Client, EmbeddingModel, Provider
from provider.openai.client import HttpClient
from provider.openai.embedding import OpenAIEmbeddingModel
from provider.openai.llm import OpenAIChatModel
from provider.types import Capability

__all__ = ["OpenAIProvider"]


class OpenAIProvider(Provider):
    """OpenAI（及其官方端点）。"""

    NAME = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    API_KEY_ENV = "OPENAI_API_KEY"
    #: 云端厂商：缺密钥必须构造期报错，而不是等第一次调用才 401（FR-P-12）
    REQUIRES_API_KEY = True

    #: 默认能力集：**只包含「对话模型默认都有的」那几项**。
    #:
    #: 刻意排除的两项，理由相同 —— 「这个具体模型能不能做这件事」不是厂商级的事实：
    #:
    #: - ``VISION``：只有部分模型支持图片。放进默认集会让「模型看不见图」的请求
    #:   被静默放行，然后产出一个**看起来正常但错误**的答案；
    #: - ``EMBEDDING``：``gpt-4o-mini`` 不能向量化。放进默认集更糟 ——
    #:   ``emb.default`` 的候选里会出现只能对话的模型，被选中后在上游报 400。
    #:
    #: 需要时在配置里显式声明：``capabilities: {vision: true}``（FR-P-08）。
    #: 注意这个类本身**仍然实现** ``embedding_model()`` —— 那是「适配器有没有这个能力」，
    #: 与「某个模型有没有这个能力」是两件事。
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
        return HttpClient(
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
        return OpenAIChatModel(self.config, self.client())

    def embedding_model(self) -> EmbeddingModel:
        return OpenAIEmbeddingModel(self.config, self.client())

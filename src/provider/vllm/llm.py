"""vLLM 对话补全。

请求形状与 OpenAI 一致（vLLM 原生提供 OpenAI 兼容的 ``/v1/chat/completions``），
所以只需要覆写**默认能力集** —— 而且是往**小**了覆写。

**为什么默认能力只有 ``runtime`` + ``stream``**：

vLLM 的能力**不取决于适配器，取决于部署时加载了什么模型、开了哪些启动参数**：

- ``tools`` 需要服务端加 ``--enable-auto-tool-choice --tool-call-parser <parser>``，
  且 parser 必须与模型家族匹配（选错会得到格式错乱的工具调用，而不是报错）；
- ``json`` 在较新版本可用，老版本完全不支持 ``response_format``；
- ``vision`` 取决于是否加载了 VL 模型。

这些都是**编译期不可能知道**的。把默认集取小，让「用不了」变成**本地拦截 + 明确报错**，
而不是一个来自服务端的、看不出原因的 400。
需要什么就在配置里开：``capabilities: {tools: true, json: true}``（FR-P-08）。
"""

from __future__ import annotations

from provider.openai.llm import OpenAIChatModel
from provider.types import Capability

__all__ = ["VLLMChatModel"]


class VLLMChatModel(OpenAIChatModel):
    """自建 vLLM 的对话模型。能力默认取最小集，由配置按部署实际情况打开。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        return frozenset({Capability.CHAT, Capability.STREAM})

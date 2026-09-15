"""DashScope 对话补全。

兼容模式下请求体与响应体形状与 OpenAI 一致，所以只覆写**能力默认集**。
这本身就是「走兼容模式」这个决策的回报：本文件短到十几行，而如果走原生协议，
这里会是一个需要单独维护的字段映射表。
"""

from __future__ import annotations

from provider.openai.llm import OpenAIChatModel
from provider.types import Capability

__all__ = ["DashScopeChatModel"]


class DashScopeChatModel(OpenAIChatModel):
    """DashScope 兼容模式的对话模型。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        # 与 OpenAI 一致不含 VISION：**能力取决于具体模型**（qwen-vl 系列才有视觉），
        # 由配置声明。放进默认集会让「模型看不见图」的请求被静默放行（FR-P-08）。
        return frozenset(
            {
                Capability.CHAT,
                Capability.STREAM,
                Capability.TOOLS,
                Capability.JSON,
            }
        )

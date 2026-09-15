"""DashScope 向量化。

兼容模式下与 OpenAI 形状一致。唯一的实际差异是 **单次请求的条数上限**
—— DashScope 比 OpenAI 严，超了直接返回 400 而不是截断。
把它做成类级默认值（而不是在 ``__init__`` 里改配置对象），
这样「调用参数 > 配置 > 厂商默认」这条优先级链天然成立：
用户显式配了 ``max_batch`` 就一定生效。
"""

from __future__ import annotations

from provider.openai.embedding import OpenAIEmbeddingModel

__all__ = ["DashScopeEmbeddingModel"]

#: DashScope 单次请求的条数上限较严（正文实测值）。保守取值，宁多分几批。
DASHSCOPE_MAX_BATCH = 25


class DashScopeEmbeddingModel(OpenAIEmbeddingModel):
    """DashScope 兼容模式的向量化模型。"""

    @classmethod
    def default_max_batch(cls) -> int:
        return DASHSCOPE_MAX_BATCH

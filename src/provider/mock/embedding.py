"""确定性假向量化。

**确定性是刻意的**：同一段文本永远得到同一个向量。
这让「写入 → 检索 → 命中」这类**跨进程、跨次运行**的链路能被稳定断言。
若用随机向量，这类测试只能靠容差蒙，且会随机红。

**但它是无语义的**：向量来自 sha256，**相似文本不会得到相近向量**。
所以它只能验证「链路通」，**不能**用来评估检索质量 ——
需要真实语义时用真实 provider。这条限制必须写在这里，
否则迟早有人拿它做召回率评测，然后得出「系统检索效果很差」的错误结论。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any

from provider.base import EmbeddingModel
from provider.types import Capability, EmbeddingResult, Usage

__all__ = ["MockEmbeddingModel"]

DEFAULT_DIMENSION = 8


class MockEmbeddingModel(EmbeddingModel):
    """基于哈希的确定性假向量化。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        return frozenset({Capability.EMBEDDING})

    def __init__(self, cfg: Any, client: Any) -> None:
        super().__init__(cfg, client)
        self.dimension = cfg.dimension or DEFAULT_DIMENSION
        self.embedded_texts: list[str] = []

    async def embed(
        self,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
        trace_id: str = "",
    ) -> EmbeddingResult:
        items = list(texts)
        self.embedded_texts.extend(items)

        vectors = tuple(self._vector(text, self.dimension) for text in items)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model_name,
            dimension=self.dimension,
            # mock 用真实形状的用量，让「成本核算」链路能被端到端测到
            usage=Usage(input_tokens=sum(len(t) for t in items) or None, output_tokens=None),
            trace_id=trace_id,
        )

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _vector(text: str, dimension: int) -> tuple[float, ...]:
        """sha256 展开 + L2 归一化。

        归一化不是装饰：真实 embedding 都是单位向量，余弦相似度才成立。
        假向量不归一化，会让任何依赖「点积即相似度」的代码在 mock 下表现正常、
        在真实 provider 下行为不同。
        """
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        counter = 0
        while len(values) < dimension:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            values.extend(byte / 255.0 - 0.5 for byte in block)
            counter += 1

        trimmed = values[:dimension]
        norm = math.sqrt(sum(value * value for value in trimmed)) or 1.0
        return tuple(value / norm for value in trimmed)

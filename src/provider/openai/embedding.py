"""OpenAI 兼容协议的向量化实现。

**两个容易写错、且写错了不会报错的地方**（本模块刻意处理）：

1. **返回顺序**。OpenAI 的 ``data`` 数组带 ``index`` 字段，且**不保证与入参同序**
   （并发批处理所致）。按数组顺序直接取，会让第 7 条的向量配到第 8 条文本上 ——
   这**不会抛任何异常**，只会让检索结果悄悄变差。
   所以这里一律按 ``index`` 重排后再返回。

2. **部分失败**（``FR-P-07``）。批量请求中某一条失败时，**整批失败并标明失败下标**，
   不得返回短一截的结果 —— 同上，短一截会引发静默错位。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from provider.base import EmbeddingModel
from provider.errors import InvalidRequestError, ProtocolError
from provider.types import Capability, EmbeddingResult, Usage

__all__ = ["OpenAIEmbeddingModel"]

_log = logging.getLogger(__name__)


class OpenAIEmbeddingModel(EmbeddingModel):
    """OpenAI 兼容形状的向量化模型。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        return frozenset({Capability.EMBEDDING})

    # ------------------------------------------------------------------ 钩子
    def _endpoint(self) -> str:
        return "/embeddings"

    def _build_payload(self, texts: Sequence[str], *, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "input": list(texts)}
        payload.update(self.config.extra)
        return payload

    def _parse(self, body: Mapping[str, Any], *, trace_id: str) -> EmbeddingResult:
        data = body.get("data")
        if not isinstance(data, Sequence) or not data:
            raise ProtocolError(
                f"向量化响应缺少 data：{str(body)[:200]}",
                provider=self.provider_name,
                model=self.model_name,
                trace_id=trace_id,
            )

        # 按 index 重排 —— 见模块 docstring 第 1 条。缺 index 时退化为原顺序。
        def _index_of(item: Any) -> int:
            try:
                return int(item.get("index"))
            except (AttributeError, TypeError, ValueError):
                return 1 << 30

        ordered = sorted(data, key=_index_of)
        vectors = tuple(
            tuple(float(x) for x in (item.get("embedding") or [])) for item in ordered
        )

        dimension = self.config.dimension or (len(vectors[0]) if vectors else 0)
        if self.config.dimension and vectors and len(vectors[0]) != self.config.dimension:
            # 维度不符必须**立刻**报错：它与向量库的 vector(N) 不对齐，
            # 而写入失败发生在很久以后的另一个模块里（FR-P-07）。
            raise ProtocolError(
                f"向量维度不符：配置声明 dimension={self.config.dimension}，"
                f"实际返回 {len(vectors[0])}。该值必须与向量库的 vector(N) 对齐。",
                provider=self.provider_name,
                model=self.model_name,
                trace_id=trace_id,
            )

        return EmbeddingResult(
            vectors=vectors,
            model=str(body.get("model") or self.model_name),
            dimension=dimension,
            usage=self._parse_usage(body.get("usage")),
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------ 解析辅助
    def _parse_usage(self, usage: Any) -> Usage:
        """OpenAI 的 embedding 只给 ``prompt_tokens``，**输出侧留 ``None``**。

        补 0 会让「成本 = 输出 token × 单价」算出 0，看起来像免费（FR-P-10）。
        """
        if not isinstance(usage, Mapping):
            return Usage()
        raw = usage.get("prompt_tokens")
        try:
            input_tokens = None if raw is None else int(raw)
        except (TypeError, ValueError):
            input_tokens = None
        return Usage(input_tokens=input_tokens, output_tokens=None)

    # ------------------------------------------------------------------ 公共管线
    async def embed(
        self,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
        trace_id: str = "",
    ) -> EmbeddingResult:
        """批量向量化。

        Args:
            texts: 待向量化的文本。空列表直接返回空结果，**不发请求**。
            batch_size: 每批条数；``None`` 时用配置的 ``max_batch``。
                分批发是因为厂商对单次请求的条数有上限，超了会直接 400。

        Raises:
            InvalidRequestError: 某一条为空（上游会因此整批失败，且报错不说是哪条）。
            ProviderError: 上游失败；消息中会带上**失败批次的起始下标**。
        """
        items = list(texts)
        if not items:
            return EmbeddingResult(
                vectors=(), model=self.model_name, dimension=self.config.dimension or 0,
                trace_id=trace_id,
            )

        for index, text in enumerate(items):
            if not str(text).strip():
                raise InvalidRequestError(
                    f"第 {index} 条待向量化文本为空；上游会因此整批失败且不指明是哪条",
                    provider=self.provider_name,
                    model=self.model_name,
                    trace_id=trace_id,
                )

        size = self.resolve_max_batch(batch_size)
        collected: list[tuple[float, ...]] = []
        usage = Usage()

        for start in range(0, len(items), size):
            batch = items[start : start + size]
            try:
                result = await self._embed_batch(batch, trace_id=trace_id)
            except Exception as exc:
                # FR-P-07：整批失败并标明**失败下标**。
                # 不这么做的后果是调用方拿到一个短列表，然后把第 7 条的向量
                # 当成第 8 条的用 —— 且永远不会报错。
                if hasattr(exc, "message"):
                    exc.message = f"{exc.message}（失败批次起始下标 {start}）"
                raise
            collected.extend(result.vectors)
            usage = usage + result.usage

        dimension = self.config.dimension or (len(collected[0]) if collected else 0)
        return EmbeddingResult(
            vectors=tuple(collected),
            model=self.model_name,
            dimension=dimension,
            usage=usage,
            trace_id=trace_id,
        )

    async def _embed_batch(self, texts: Sequence[str], *, trace_id: str) -> EmbeddingResult:
        payload = self._build_payload(texts, model=self.model_name)
        body = await self._client.post_json(
            self._endpoint(), payload, timeout_s=self.config.timeout_s, trace_id=trace_id
        )
        result = self._parse(body, trace_id=trace_id)

        # 数量对不上说明上游有问题（或我们的 index 重排出了错）。**必须报错** ——
        # 少了或多了一条都会让后续所有向量错位。
        if len(result.vectors) != len(texts):
            raise ProtocolError(
                f"返回向量数 {len(result.vectors)} 与请求条数 {len(texts)} 不符",
                provider=self.provider_name,
                model=self.model_name,
                trace_id=trace_id,
            )
        return result

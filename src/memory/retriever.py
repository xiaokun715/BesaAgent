"""混合检索：**向量 + 结构化过滤 + 关键词**（`FR-M-05`）。

**为什么不是纯向量**：向量对**专有名词与代码标识符不敏感** ——
「`besa_agent`」「`pytest.ini`」「`FR-M-16`」这类词，向量空间里几乎不可分，
但它们恰恰是工程记忆里最该被精确命中的东西。关键词这一路就是补它的。

**为什么结构化过滤是「底座」而不是「选项」**：``state='active'`` 与作用域
**每一个查询都必须带**（`NFR-M-09` / `NFR-M-03`）。把它们交给调用方传，
迟早会有人漏掉 —— 而漏掉的状态过滤会把过时记忆当事实返回，
漏掉的作用域过滤会把**别人的记忆**返回。所以它们写在仓储的必经路径上，
本模块不（也不能）绕开。

**融合方式**：分数 = ``(1-w)·向量相似度 + w·关键词命中``。
用固定权重而不是 RRF，是因为这里两路的量纲都可以归一到 ``[0,1]``，
而固定权重**完全确定**（`NFR-M-06`）—— 便于回归测试。
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from memory.store import MemoryStore
from memory.types import (
    Embedder,
    MemoryContext,
    MemoryItem,
    RetrievalHit,
)

__all__ = ["RetrievalPolicy", "Retriever", "lexical_overlap"]

_log = logging.getLogger(__name__)

_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[一-鿿]")


@dataclass(frozen=True)
class RetrievalPolicy:
    """检索参数（``memory.retrieve`` + ``memory.write.retrieve`` 段）。"""

    #: 默认取回条数（调用方可以再收窄，但**不能超过** ``max_results``）。
    top_k: int = 10
    #: 硬上限。结果直接进上下文，必须有界（`NFR-M-08`）。
    max_results: int = 20
    #: 检索结果的字节上限。超了就截断，并**如实标注**。
    max_bytes: int = 32_768
    #: 高于它才算「高度相似」（写入路径 → 强化）。
    similarity_threshold: float = 0.90
    #: 高于它才算「相同」（写入路径 → 合并）。**必须 ≥ similarity**（`C-M-4`）。
    merge_threshold: float = 0.96
    #: 向量检索的过采样倍数：可见性过滤会刷掉一部分，先多取一些。
    oversample: float = 4.0
    #: 关键词在融合分里的权重。
    lexical_weight: float = 0.3

    @classmethod
    def from_config(
        cls,
        retrieve_cfg: Mapping[str, Any] | None,
        write_cfg: Mapping[str, Any] | None = None,
    ) -> "RetrievalPolicy":
        data = dict(retrieve_cfg or {})
        write = dict(write_cfg or {})
        policy = cls(
            top_k=int(write.get("top_k", 10)),
            max_results=int(data.get("max_results", 20)),
            max_bytes=int(data.get("max_bytes", 32_768)),
            similarity_threshold=float(write.get("similarity_threshold", 0.90)),
            merge_threshold=float(write.get("merge_threshold", 0.96)),
            oversample=float(data.get("oversample", 4.0)),
            lexical_weight=float(data.get("lexical_weight", 0.3)),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.max_results <= 0 or self.top_k <= 0:
            raise ValueError("memory.retrieve 的 top_k / max_results 必须为正")
        if self.merge_threshold < self.similarity_threshold:
            # `C-M-4`：合并比强化更保守 —— 配置层挡住写反的情况。
            # 写反的后果是「该强化的被合并了」，而合并会丢信息且无人察觉。
            raise ValueError(
                f"memory.write.retrieve.merge_threshold({self.merge_threshold}) "
                f"必须 ≥ similarity_threshold({self.similarity_threshold})"
            )
        if not 0.0 <= self.lexical_weight <= 1.0:
            raise ValueError(
                f"memory.retrieve.lexical_weight 必须在 [0,1]，得到 {self.lexical_weight}"
            )


class Retriever:
    """混合检索器。**每个事务里构造一次**（它只持有无状态依赖）。"""

    __slots__ = ("_embedder", "_policy", "_model_key")

    def __init__(
        self,
        embedder: Embedder | None,
        policy: RetrievalPolicy,
        *,
        model_key: str = "",
    ) -> None:
        self._embedder = embedder
        self._policy = policy
        self._model_key = model_key

    @property
    def policy(self) -> RetrievalPolicy:
        return self._policy

    @property
    def has_vectors(self) -> bool:
        return self._embedder is not None

    async def retrieve(
        self,
        store: MemoryStore,
        *,
        query: str,
        ctx: MemoryContext,
        kinds: Sequence[str] | None = None,
        top_k: int | None = None,
        exclude_ids: Sequence[UUID] = (),
        now: datetime | None = None,
        bounded: bool = True,
    ) -> tuple[RetrievalHit, ...]:
        """取回与 ``query`` 最相关的若干条**当前有效**的记忆。

        Args:
            bounded: 是否施加上限（条数 + 字节）。**写入路径上的那次检索
                用 ``bounded=False``** —— 它只是想找几个「对手」来判冲突，
                截断反而会漏掉该比较的那条。
        """
        policy = self._policy
        wanted = min(top_k or policy.top_k, policy.max_results) if bounded else (top_k or policy.top_k)
        oversample = max(int(wanted * policy.oversample), wanted)

        vector = await self._query_vector(query)

        rows = await store.search(
            ctx,
            kinds=kinds,
            query_vector=vector,
            top_k=oversample,
            exclude_ids=exclude_ids,
            now=now,
        )

        hits: list[RetrievalHit] = []
        for item, distance in rows:
            vector_score = _distance_to_score(distance) if distance is not None else 0.0
            lexical = lexical_overlap(query, item.content)
            if vector is None:
                # 没有向量这一路：分数就完全由关键词决定。这不是「降级到 0」——
                # 结构化 + 关键词本身就是一条**完整**的检索路径，不是残废版。
                score = lexical
            else:
                score = (1.0 - policy.lexical_weight) * vector_score + policy.lexical_weight * lexical
            hits.append(RetrievalHit(item=item, score=score, distance=distance, lexical=lexical))

        hits.sort(key=lambda hit: (-hit.score, -(hit.item.weight or 0.0)))
        hits = hits[:wanted]
        if bounded:
            hits = _cap_bytes(hits, policy.max_bytes)
        return tuple(hits)

    async def _query_vector(self, query: str) -> tuple[float, ...] | None:
        """把查询向量化。**失败不抛** —— 退到关键词路径并出声。"""
        if self._embedder is None or not query.strip():
            return None
        try:
            vectors = await self._embedder.embed([query])
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆检索的向量化失败，本次退到关键词 + 结构化路径：%s", exc)
            return None
        if not vectors:
            return None
        return tuple(float(x) for x in vectors[0])


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def lexical_overlap(query: str, text: str) -> float:
    """关键词命中度，Jaccard 相似（``|A∩B| / |A∪B|``）。

    **中英分开处理**：英文/标识符按词切，中文按**字符二元组** ——
    中文没有空格，按词切需要分词器（依赖太重），而二元组对「超时 30s」
    这类短记忆已经够用。
    """
    left = _tokens(query)
    right = _tokens(text)
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    union = len(left | right)
    return intersection / union if union else 0.0


def _tokens(text: str) -> set[str]:
    lowered = text.casefold()
    words = {match.group(0) for match in _TOKEN.finditer(lowered)}
    chars = [ch for ch in lowered if _CJK.match(ch)]
    bigrams = {chars[index] + chars[index + 1] for index in range(len(chars) - 1)}
    return words | bigrams


def _distance_to_score(distance: float) -> float:
    """把向量距离转成 ``[0,1]`` 的相关度。

    默认按 **cosine** 解释：``sim = 1 - distance``，再夹到 ``[0,1]``。
    换成 l2 / inner_product 时这个换算不再精确 —— 但它的**单调性**不变，
    而排序只看单调性；阈值判定的精度问题记在 ``MQ-3`` 里等实测。
    """
    if math.isnan(distance) or math.isinf(distance):
        return 0.0
    return min(1.0, max(0.0, 1.0 - distance))


def _cap_bytes(hits: list[RetrievalHit], max_bytes: int) -> list[RetrievalHit]:
    """按字节上限截断。**保留高分的那几条**，低分的整条丢弃而不是切半个。"""
    kept: list[RetrievalHit] = []
    total = 0
    for hit in hits:
        size = len(hit.item.content.encode("utf-8"))
        if kept and total + size > max_bytes:
            break
        kept.append(hit)
        total += size
    return kept

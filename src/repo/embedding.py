"""向量能力的**协议与载体**（`FR-R-08`）。具体实现在 ``apps/*/storage``。

**为什么协议在 repo、实现在 app 层**：``vector(N)``、``<=>``、HNSW 都是
PostgreSQL + pgvector 特有的 SQL —— 属于「必须绑定具体后端」的东西，
按 ``apps/*/storage`` 的边界划分它归 app 层。而 ``src/`` 不能 import ``apps/``，
所以这里只声明**形状**，实现由组合根注入（与 ``Database`` 完全同构）。

**为什么方法收一个 ``tx`` 参数**：向量的写入**必须与它的业务行在同一个事务**
（`FR-R-08` / `FR-M-17`）。把 ``tx`` 显式传进来，是让「这次写向量用的是哪个事务」
一眼可见 —— 若让 ``VectorStore`` 自己持有连接，就没有任何东西拦得住
「记忆行提交了、向量没提交」这种半截状态。

**为什么检索返回距离而不只是 top-k**：上层要据此决定「用不用」
（相关度不够宁可不用），而不是拿到一个「就是这几条」的列表（`FR-R-08`）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["VectorHit", "VectorRecord", "VectorStore"]


@dataclass(frozen=True)
class VectorRecord:
    """一条待写入的向量。

    ``owner`` 表达「这个向量属于谁」——``(kind, id)`` 而不是单个外键，
    是因为同一张向量表要服务多种实体（记忆 / 消息 / 文档），
    按维度分表（``DR-4``）之后它们的列形状完全一致，只有 owner 不同。
    """

    space: str
    owner_kind: str
    owner_id: str
    embedding: tuple[float, ...]
    model_key: str = ""

    def __post_init__(self) -> None:
        if not self.space.strip():
            raise ValueError("向量记录必须给出 space")
        if not self.owner_id:
            raise ValueError("向量记录必须给出 owner_id")
        if not self.embedding:
            raise ValueError("向量记录不能是空向量")


@dataclass(frozen=True)
class VectorHit:
    """一次向量检索的命中。``distance`` 的语义由向量空间的 ``metric`` 决定。"""

    owner_kind: str
    owner_id: str
    distance: float


@runtime_checkable
class VectorStore(Protocol):
    """向量存储的协议。**实现绑定 pgvector，由组合根注入。**"""

    async def upsert(self, tx, records: Sequence[VectorRecord]) -> int:
        """写入或覆盖向量。返回写入条数。

        覆盖语义由 ``uniq(space, owner_kind, owner_id)`` 保证 ——
        一条记忆换了 embedding 模型时，是**替换**而不是并存两条。
        """
        ...

    async def delete(self, tx, *, space: str, owner_kind: str, owner_ids: Sequence[str]) -> int:
        """删除向量。**硬删除记忆时必须连它一起删**（`FR-M-03`）——
        向量留在库里 = 内容没删干净。
        """
        ...

    async def search(
        self,
        tx,
        *,
        space: str,
        vector: Sequence[float],
        top_k: int,
        owner_kind: str | None = None,
        owner_ids: Sequence[str] | None = None,
    ) -> tuple[VectorHit, ...]:
        """最近邻检索。``owner_ids`` 用于「只在这些候选里找」的收窄。"""
        ...

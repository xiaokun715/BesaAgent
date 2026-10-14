"""领域形态 ↔ 存储载体的适配（`NFR-R-03` 的必然产物）。

**为什么需要这一层**：``src/repo`` **不得 import ``memory``** —— 它在依赖链上更下游。
所以 :class:`~repo.memory.MemoryRepo` 收发的必须是**存储无关的**
:class:`~repo.memory.MemoryView`，而本模块用的是 :class:`~memory.types.MemoryItem`。
两个类型一一对应但**不相等**，映射必须发生在某一处 —— 收在这里，
``manager`` 就不必在每个方法里手写字段搬运。

**``content_hash`` 的规范化只做「跨重试必然一致」的部分**（空白、大小写），
**不做语义归一化**（不把「超时 30s」与「超时三十秒」看成同一条）——
与工具幂等的同一条纪律：去重失败是安全的，误去重是危险的。
语义等价交给 :mod:`memory.resolver` 的 LLM 判定。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from memory.types import MemoryContext, MemoryItem, MemorySource
from repo.memory import MemoryRepo, MemoryView

__all__ = ["MemoryStore", "content_hash", "item_from_view", "view_from_item"]


def content_hash(content: str, scope: str) -> str:
    """内容指纹。``hash(规范化内容 + 作用域)``。

    带作用域是因为「同一句话在不同作用域里是不同的记忆」——
    与工具幂等键里带 scope 是同一条理由。
    """
    normalized = " ".join(content.split()).casefold()
    digest = hashlib.sha256(f"{scope}\n{normalized}".encode("utf-8")).hexdigest()
    return digest


def view_from_item(item: MemoryItem, *, content_hash_override: str | None = None) -> MemoryView:
    return MemoryView(
        memory_id=str(item.memory_id),
        kind=item.kind,
        content=item.content,
        scope=item.scope,
        state=item.state,
        tenant_id=item.tenant_id,
        owner_id=item.owner_id,
        summary=item.summary,
        content_hash=content_hash_override or content_hash(item.content, item.scope),
        category=item.category,
        tags=tuple(item.tags),
        sensitivity=item.sensitivity,
        source_kind=item.source.kind,
        source_session_id=item.source.session_id,
        source_agent=item.source.agent,
        source_trace_id=item.source.trace_id,
        session_id=item.source.session_id,
        caller=item.source.caller,
        confidence=item.confidence,
        importance=item.importance,
        weight=item.weight,
        version=item.version,
        supersedes_id=str(item.supersedes_id) if item.supersedes_id else None,
        merged_into=str(item.merged_into) if item.merged_into else None,
        access_count=item.access_count,
        used_count=item.used_count,
        evidence_count=item.evidence_count,
        valid_from=item.valid_from,
        valid_to=item.valid_to,
        created_at=item.created_at,
        updated_at=item.updated_at,
        last_accessed_at=item.last_accessed_at,
        last_decayed_at=item.last_decayed_at,
        metadata=dict(item.metadata),
    )


def item_from_view(view: MemoryView) -> MemoryItem:
    return MemoryItem(
        memory_id=_as_uuid(view.memory_id),
        kind=view.kind,  # type: ignore[arg-type]
        content=view.content,
        scope=view.scope,  # type: ignore[arg-type]
        state=view.state,  # type: ignore[arg-type]
        tenant_id=view.tenant_id,
        owner_id=view.owner_id,
        summary=view.summary,
        category=view.category,
        tags=tuple(view.tags),
        sensitivity=view.sensitivity,
        confidence=view.confidence,
        importance=view.importance,
        weight=view.weight,
        source=MemorySource(
            kind=view.source_kind,
            session_id=view.source_session_id,
            agent=view.source_agent,
            trace_id=view.source_trace_id,
            caller=view.caller,
        ),
        version=view.version,
        supersedes_id=_as_uuid(view.supersedes_id),
        merged_into=_as_uuid(view.merged_into),
        access_count=view.access_count,
        used_count=view.used_count,
        evidence_count=view.evidence_count,
        valid_from=view.valid_from,
        valid_to=view.valid_to,
        created_at=view.created_at,
        updated_at=view.updated_at,
        last_accessed_at=view.last_accessed_at,
        last_decayed_at=view.last_decayed_at,
        metadata=dict(view.metadata),
    )


class MemoryStore:
    """把 ``MemoryRepo`` 包成**领域类型进出**的门面。

    每个事务里构造一个（与仓储同生命周期）。它不做判定、不做检索排序 ——
    那些是 ``policy`` / ``retriever`` / ``resolver`` 的事；它只负责
    「把领域对象翻译成存储能懂的形状，再翻译回来」。
    """

    __slots__ = ("_repo", "_model_key")

    def __init__(self, repo: MemoryRepo, *, model_key: str = "") -> None:
        self._repo = repo
        self._model_key = model_key

    @property
    def repo(self) -> MemoryRepo:
        return self._repo

    # ---------------------------------------------------------------- 写
    async def save(
        self, item: MemoryItem, *, embedding: Sequence[float] | None = None
    ) -> MemoryItem:
        view = await self._repo.insert(
            view_from_item(item), embedding=embedding, model_key=self._model_key
        )
        return item_from_view(view)

    async def reinforce(
        self, memory_id: UUID, *, weight: float, used: bool, evidence_inc: int = 1,
        now: datetime | None = None,
    ) -> None:
        await self._repo.reinforce(
            str(memory_id), weight=weight, evidence_inc=evidence_inc, used=used, now=now
        )

    async def mark_accessed(self, memory_id: UUID, *, now: datetime | None = None) -> None:
        await self._repo.mark_accessed(str(memory_id), now=now)

    async def supersede(self, old_id: UUID, new_id: UUID, *, now: datetime | None = None) -> None:
        await self._repo.supersede(str(old_id), str(new_id), now=now)

    async def merge(self, source_ids: Sequence[UUID], target_id: UUID, *, now: datetime | None = None) -> None:
        await self._repo.mark_merged([str(x) for x in source_ids], str(target_id), now=now)

    async def revive(self, memory_id: UUID, *, weight: float, now: datetime | None = None) -> None:
        await self._repo.revive(str(memory_id), weight=weight, now=now)

    async def tombstone(self, memory_id: UUID, *, now: datetime | None = None) -> None:
        await self._repo.set_state(str(memory_id), "tombstone", now=now)

    async def hard_delete(self, memory_id: UUID) -> bool:
        return await self._repo.hard_delete(str(memory_id))

    async def append_op(self, **kwargs: Any) -> None:
        await self._repo.append_op(**kwargs)

    # ---------------------------------------------------------------- 读
    async def find_by_id(self, memory_id: UUID) -> MemoryItem | None:
        view = await self._repo.find_by_id(str(memory_id))
        return item_from_view(view) if view is not None else None

    async def find_by_hash(self, digest: str, ctx: MemoryContext) -> MemoryItem | None:
        view = await self._repo.find_by_hash(digest, scope=ctx.scope, tenant_id=ctx.tenant_id)
        return item_from_view(view) if view is not None else None

    async def find_tombstone(self, digest: str, ctx: MemoryContext) -> MemoryItem | None:
        view = await self._repo.find_tombstone(digest, scope=ctx.scope, tenant_id=ctx.tenant_id)
        return item_from_view(view) if view is not None else None

    async def search(
        self,
        ctx: MemoryContext,
        *,
        kinds: Sequence[str] | None = None,
        query_vector: Sequence[float] | None = None,
        top_k: int = 10,
        exclude_ids: Sequence[UUID] = (),
        now: datetime | None = None,
    ) -> list[tuple[MemoryItem, float | None]]:
        rows = await self._repo.search(
            tenant_id=ctx.tenant_id,
            scope=ctx.scope,
            owner_id=ctx.owner_id,
            kinds=kinds,
            query_vector=query_vector,
            top_k=top_k,
            exclude_ids=[str(x) for x in exclude_ids],
            now=now,
        )
        return [(item_from_view(view), distance) for view, distance in rows]

    async def sweep_candidates(
        self, *, limit: int, offset: int = 0, tenant_id: str = "", scope: str = "",
        older_than: datetime | None = None, now: datetime | None = None,
    ) -> list[MemoryItem]:
        views = await self._repo.sweep_candidates(
            limit=limit, offset=offset, tenant_id=tenant_id, scope=scope,
            older_than=older_than, now=now,
        )
        return [item_from_view(view) for view in views]

    async def cluster_members(
        self, *, kinds: Sequence[str], limit: int, tenant_id: str = "", scope: str = ""
    ) -> list[MemoryItem]:
        views = await self._repo.cluster_members(
            tenant_id=tenant_id, scope=scope, kinds=kinds, limit=limit
        )
        return [item_from_view(view) for view in views]

    async def trajectory(
        self, *, tenant_id: str = "", scope: str = "", since: datetime | None = None,
        kinds: Sequence[str] | None = None, limit: int = 200, now: datetime | None = None,
    ) -> list[MemoryItem]:
        views = await self._repo.trajectory(
            tenant_id=tenant_id, scope=scope, since=since, kinds=kinds, limit=limit, now=now
        )
        return [item_from_view(view) for view in views]

    async def last_maintenance_at(self) -> datetime | None:
        return await self._repo.last_maintenance_at()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _as_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        return None

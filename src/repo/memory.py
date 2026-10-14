"""记忆的**元数据与操作轨迹**（`FR-R-04` / `FR-R-08` 的落库侧）。

**为什么在 repo 而不是 ``src/memory/`` 自持**（与 ``tool_execution`` 同一条）：
它是**基础设施数据** —— 一条记忆什么时候写的、被谁改过、现在作不作数 ——
与 ``event`` / ``usage`` / ``checkpoint`` 同类。``FR-R-09`` 的「业务模块自持表」
是为「测试用例 / 缺陷」那类**领域产物**设的。

**为什么表只有两张，而需求写了六组**（架构文档 §4.3）：
六组是**逻辑分组**。拆成六张表意味着**每一次检索都要三次 join**，而检索在热路径上。
向量必须独立（``vector(N)`` 把维度钉在列类型上，``DR-4``），
轨迹必须独立（append-only，与可变主体分离才不会被 update 污染）——
除此之外的「核心 / 分类 / 版本 / 热度」都只是 ``memory`` 表的列。

**本模块不 import ``memory``**（`NFR-R-03` 硬约束）：所以它收到和返回的是
**存储无关的** :class:`MemoryView`，而不是 ``memory.MemoryItem``。
领域形态的映射发生在 ``src/memory/store.py``。

**``content_hash`` 是幂等的锚**：``hash(规范化后的内容 + 作用域)``。
规范化只做「跨重试必然一致」的部分（空白、大小写），**不做语义归一化** ——
与工具幂等的同一条纪律：去重失败是安全的，误去重是危险的。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from foundation.db import Base, BigIntPk
from repo.base import Repository
from repo.embedding import VectorRecord

__all__ = ["MemoryOpRow", "MemoryRepo", "MemoryRow", "MemoryView"]

_log = logging.getLogger(__name__)

#: JSON 列必须**显式**给出类型（``mapped_column(JSON, …)``），
#: 否则 SQLAlchemy 会抛 ``MappedAnnotationError`` —— ``checkpoint`` / ``event``
#: 都踩过这个，这里收成一个常量，免得新表忘了写。
JSON_TYPE = JSON

#: 只读视图里出现向量的默认空间名。没有向量能力时也用它占位。
DEFAULT_VECTOR_SPACE = "default"


@dataclass(frozen=True)
class MemoryView:
    """一条记忆的**存储无关载体**。

    与 ``tool`` 的 ``ExecutionView`` 同一条理由：``repo`` 返回视图而不是 ORM 行，
    「领域层只是读」这件事才是**结构上**成立的 —— 直接交出 ORM 对象，
    脏跟踪会在下一次 flush 时把读取时的判定悄悄写回去。

    **字段与 ``memory.MemoryItem`` 一一对应但不相等**：repo 不认识上层的类型。
    """

    memory_id: str
    kind: str
    content: str
    scope: str = "session"
    state: str = "active"

    tenant_id: str = ""
    owner_id: str = ""
    summary: str = ""
    content_hash: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    sensitivity: str = ""

    source_kind: str = "conversation"
    source_session_id: str | None = None
    source_agent: str | None = None
    source_trace_id: str = ""
    session_id: str | None = None
    caller: str | None = None

    confidence: float = 0.5
    importance: float = 0.5
    weight: float = 1.0
    version: int = 1
    schema_version: int = 1

    supersedes_id: str | None = None
    superseded_by: str | None = None
    merged_into: str | None = None

    access_count: int = 0
    used_count: int = 0
    evidence_count: int = 0

    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    last_decayed_at: datetime | None = None

    metadata: Mapping[str, Any] = field(default_factory=dict)


class MemoryRow(Base):
    """一条记忆。**核心表示 / 分类属性 / 版本时效 / 访问热度都在这一行上**。"""

    __tablename__ = "memory"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    # ---- ① 基础组件与租户隔离 ------------------------------------------
    #: 业务主键。存成 ``Text`` 而不是原生 uuid —— 与 ``event_id`` / ``run_id``
    #: 保持同一约定，且 SQLite（单测）上行为一致。
    memory_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default="session", nullable=False)
    owner_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # ---- ② 核心记忆表示 ------------------------------------------------
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 幂等的锚：内容 + 作用域的指纹（见模块 docstring）。
    content_hash: Mapped[str] = mapped_column(Text, default="", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ---- ③ 记忆分类与属性 ----------------------------------------------
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tags: Mapped[Sequence[str]] = mapped_column(JSON_TYPE, default=list, nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, default="conversation", nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_trace_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)

    # ---- ⑤ 版本与时效控制 ----------------------------------------------
    #: 只有 ``active`` 会被检索返回（架构文档 §2.3）。
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    merged_into: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 硬时效。``task_state`` 是「到点即错」，不靠衰减（`FR-M-07`）。
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- ⑥ 访问热度 ----------------------------------------------------
    #: ``access_count`` 记「被取出来过几次」；``used_count`` 记「真的被采用过几次」。
    #: **强化只看后者** —— 命中就强化会让错误记忆自我升权（`FR-M-08`）。
    access_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_decayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- 归属与时间 ----------------------------------------------------
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    caller: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta: Mapped[Mapping[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("memory_id", name="uq_memory_memory_id"),
        # 检索的底座条件：租户 + 作用域 + 种类 + 状态。
        # 列顺序按「区分度从高到低」排，让过滤尽量走到索引前缀。
        Index("ix_memory_scope_kind_state", "tenant_id", "scope", "kind", "state", "created_at"),
        # 离线衰减扫描：找「长期没人访问的 active」。
        Index("ix_memory_state_accessed", "state", "last_accessed_at"),
        # 幂等查重：同一个作用域里有没有同内容指纹。
        Index("ix_memory_hash", "scope", "content_hash"),
        Index("ix_memory_source_session", "source_session_id"),
    )

    def __repr__(self) -> str:
        return f"MemoryRow(kind={self.kind!r}, state={self.state!r}, id={self.memory_id[:8]!r})"


class MemoryOpRow(Base):
    """记忆的操作轨迹。**append-only**（与 ``event`` 表同级纪律）。

    它同时承担三件事：**审计**（谁改的）、**反思的输入**（轨迹）、
    **回滚的依据**（``before_hash``）。所以它只增不改不删。

    ``memory_id`` **可空**：离线维护的汇总行（一次衰减扫描）不属于任何单条记忆，
    但它同样是「发生过什么」的一部分 —— 而且它是维护游标的来源。
    """

    __tablename__ = "memory_op_log"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    op_id: Mapped[str] = mapped_column(Text, nullable=False)
    memory_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    op: Mapped[str] = mapped_column(String(16), nullable=False)

    before_hash: Mapped[str] = mapped_column(Text, default="", nullable=False)
    after_hash: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    actor: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_trace_id: Mapped[str] = mapped_column(Text, default="", nullable=False)

    scope: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload: Mapped[Mapping[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("op_id", name="uq_memory_op_log_op_id"),
        Index("ix_memory_op_memory_time", "memory_id", "occurred_at"),
        Index("ix_memory_op_op_time", "op", "occurred_at"),
        Index("ix_memory_op_scope_time", "scope", "occurred_at"),
    )


class MemoryRepo(Repository):
    """记忆的落库侧。

    **写入与向量在同一个事务**（`FR-M-17`）：``insert`` 拿到 ``tx`` 之后，
    先写 ``memory`` 行、再经注入的 ``VectorStore`` 写向量 ——
    两者共用本仓储所属的同一个事务对象。
    """

    def __init__(self, tx, vector_store: Any | None = None, *, space: str = DEFAULT_VECTOR_SPACE) -> None:
        super().__init__(tx)
        self._vectors = vector_store
        self._space = space

    @property
    def has_vectors(self) -> bool:
        return self._vectors is not None

    # ---------------------------------------------------------------- 写
    async def insert(
        self,
        view: MemoryView,
        *,
        embedding: Sequence[float] | None = None,
        model_key: str = "",
    ) -> MemoryView:
        """写入一条记忆，**并（如果有向量能力）在同一事务写它的向量**。

        ``embedding`` 为 ``None`` 时只写元数据 —— 这**不是**推荐路径，
        而是「没有向量能力」时的降级：记忆仍可被结构化 / 关键词检索到。
        """
        now = view.created_at or _utcnow()
        row = MemoryRow(
            memory_id=view.memory_id,
            tenant_id=view.tenant_id,
            scope=view.scope,
            owner_id=view.owner_id,
            schema_version=view.schema_version,
            content=view.content,
            content_hash=view.content_hash,
            summary=view.summary,
            kind=view.kind,
            category=view.category,
            tags=list(view.tags),
            sensitivity=view.sensitivity,
            source_kind=view.source_kind,
            source_session_id=view.source_session_id,
            source_agent=view.source_agent,
            source_trace_id=view.source_trace_id,
            confidence=view.confidence,
            importance=view.importance,
            state=view.state,
            version=view.version,
            supersedes_id=view.supersedes_id,
            superseded_by=view.superseded_by,
            merged_into=view.merged_into,
            valid_from=view.valid_from,
            valid_to=view.valid_to,
            access_count=view.access_count,
            used_count=view.used_count,
            evidence_count=view.evidence_count,
            weight=view.weight,
            last_accessed_at=view.last_accessed_at,
            last_decayed_at=view.last_decayed_at,
            session_id=view.session_id,
            caller=view.caller,
            meta=dict(view.metadata),
            created_at=now,
            updated_at=now,
        )
        self.tx.add(row)
        await self.tx.flush()

        if embedding is not None and self._vectors is not None:
            await self._vectors.upsert(
                self.tx,
                [
                    VectorRecord(
                        space=self._space,
                        owner_kind="memory",
                        owner_id=view.memory_id,
                        embedding=tuple(embedding),
                        model_key=model_key,
                    )
                ],
            )
        elif embedding is not None:
            # 有向量却存不下去，是「内容与它的向量没同事务」的前兆 —— 必须出声。
            _log.warning(
                "记忆 %s 拿到了向量但没有 VectorStore，向量**未写入** —— "
                "它会退化成只能靠结构化/关键词检索。",
                view.memory_id[:8],
            )
        return _to_view(row)

    async def append_op(
        self,
        *,
        op_id: str,
        op: str,
        memory_id: str | None = None,
        before_hash: str = "",
        after_hash: str = "",
        reason: str = "",
        actor: str = "",
        source_trace_id: str = "",
        scope: str = "",
        tenant_id: str = "",
        payload: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """追加一条操作轨迹。**它永不更新、永不删除。**"""
        self.tx.add(
            MemoryOpRow(
                op_id=op_id,
                memory_id=memory_id,
                op=op,
                before_hash=before_hash,
                after_hash=after_hash,
                reason=reason,
                actor=actor,
                source_trace_id=source_trace_id,
                scope=scope,
                tenant_id=tenant_id,
                payload=dict(payload or {}),
                occurred_at=now or _utcnow(),
            )
        )
        await self.tx.flush()

    async def reinforce(
        self,
        memory_id: str,
        *,
        weight: float,
        evidence_inc: int = 1,
        used: bool = False,
        now: datetime | None = None,
    ) -> int:
        """反哺：上调权重、累加证据、刷新访问时间。返回 ``used_count`` 增量。

        ``used=True`` 才动 ``used_count`` —— 「被取出来」与「被采用」是两回事
        （`FR-M-08`）。
        """
        row = await self._load(memory_id)
        if row is None:
            return 0
        moment = now or _utcnow()
        row.weight = weight
        row.access_count += 1
        row.evidence_count += evidence_inc
        row.last_accessed_at = moment
        row.updated_at = moment
        if used:
            row.used_count += 1
        await self.tx.flush()
        return 1 if used else 0

    async def mark_accessed(self, memory_id: str, *, now: datetime | None = None) -> None:
        """只记「被取出来过」。**不动权重**（`FR-M-08`）。"""
        row = await self._load(memory_id)
        if row is None:
            return
        row.access_count += 1
        row.last_accessed_at = now or _utcnow()
        await self.tx.flush()

    async def supersede(
        self, old_id: str, new_id: str, *, now: datetime | None = None
    ) -> None:
        """把旧记忆标成「被新版本取代」。**旧行保留**，只是不再被检索（`FR-M-02`）。"""
        old = await self._load(old_id)
        if old is None:
            return
        moment = now or _utcnow()
        old.state = "superseded"
        old.superseded_by = new_id
        old.valid_to = moment
        old.updated_at = moment
        await self.tx.flush()

    async def mark_merged(
        self, source_ids: Sequence[str], target_id: str, *, now: datetime | None = None
    ) -> None:
        """把若干条标成「已并入 target」。证据不丢，只是换了归属（`FR-M-04`）。"""
        moment = now or _utcnow()
        for source_id in source_ids:
            if source_id == target_id:
                continue
            row = await self._load(source_id)
            if row is None:
                continue
            row.state = "merged"
            row.merged_into = target_id
            row.valid_to = moment
            row.updated_at = moment
        await self.tx.flush()

    async def revive(
        self, memory_id: str, *, weight: float, now: datetime | None = None
    ) -> None:
        """把 ``tombstone`` 复活成 ``active``（同内容被重新学到，`MQ-6`）。

        保留原 ``memory_id`` 与它的轨迹 —— 建新行会把这条记忆的历史切断，
        而「它曾经被遗忘过」本身就是有用的事实。
        """
        row = await self._load(memory_id)
        if row is None:
            return
        moment = now or _utcnow()
        row.state = "active"
        row.weight = weight
        row.valid_to = None
        row.last_accessed_at = moment
        row.updated_at = moment
        await self.tx.flush()

    async def set_state(
        self, memory_id: str, state: str, *, now: datetime | None = None
    ) -> None:
        row = await self._load(memory_id)
        if row is None:
            return
        row.state = state
        row.updated_at = now or _utcnow()
        await self.tx.flush()

    async def hard_delete(self, memory_id: str) -> bool:
        """硬删除：行**与它的向量**一起删（`FR-M-03`）。

        向量留在库里 = 内容没删干净 —— 而硬删除正是为了「内容必须消失」，
        所以这两步**必须一起**（向量删除也走同一个事务）。
        """
        row = await self._load(memory_id)
        if row is None:
            return False
        if self._vectors is not None:
            await self._vectors.delete(
                self.tx, space=self._space, owner_kind="memory", owner_ids=[memory_id]
            )
        await self.tx.delete(row)
        await self.tx.flush()
        return True

    # ---------------------------------------------------------------- 读
    async def find_by_id(self, memory_id: str) -> MemoryView | None:
        row = await self._load(memory_id)
        return _to_view(row) if row is not None else None

    async def find_by_hash(
        self, content_hash: str, *, scope: str, tenant_id: str
    ) -> MemoryView | None:
        """按内容指纹找一条**未退场**的记忆（幂等查重，`FR-M-01`）。

        只在 ``active`` 里找：``tombstone`` 的同内容应走「复活」而不是「当作已存在」。
        """
        row = await self.tx.fetch_one(
            select(MemoryRow)
            .where(
                MemoryRow.content_hash == content_hash,
                MemoryRow.scope == scope,
                MemoryRow.tenant_id == tenant_id,
                MemoryRow.state == "active",
            )
            .order_by(MemoryRow.updated_at.desc())
            .limit(1)
        )
        return _to_view(row) if row is not None else None

    async def find_tombstone(
        self, content_hash: str, *, scope: str, tenant_id: str
    ) -> MemoryView | None:
        """按指纹找一条已遗忘的记忆，供「复活」使用。"""
        row = await self.tx.fetch_one(
            select(MemoryRow)
            .where(
                MemoryRow.content_hash == content_hash,
                MemoryRow.scope == scope,
                MemoryRow.tenant_id == tenant_id,
                MemoryRow.state == "tombstone",
            )
            .order_by(MemoryRow.updated_at.desc())
            .limit(1)
        )
        return _to_view(row) if row is not None else None

    async def search(
        self,
        *,
        tenant_id: str,
        scope: str,
        owner_id: str = "",
        kinds: Sequence[str] | None = None,
        query_vector: Sequence[float] | None = None,
        top_k: int = 10,
        exclude_ids: Sequence[str] = (),
        now: datetime | None = None,
    ) -> list[tuple[MemoryView, float | None]]:
        """检索候选。返回 ``(记忆, 向量距离)`` —— 距离为 ``None`` 表示没走向量。

        **``active`` 与作用域是每个查询的底座条件**，不交给调用方传
        （`NFR-M-09` / `NFR-M-03`）：漏一个就会把过时的、甚至别的租户的记忆
        取回来，而那种错误在测试里很难复现。

        向量路径走「先向量收窄 owner、再按底座条件加载」两步：
        向量检索只认 ``owner_id``，可见性判定**必须**回到关系行上来做。
        """
        moment = now or _utcnow()
        candidates: dict[str, float | None] = {}

        if query_vector is not None and self._vectors is not None:
            hits = await self._vectors.search(
                self.tx,
                space=self._space,
                vector=tuple(query_vector),
                top_k=max(top_k * 4, top_k),  # 过采样：可见性过滤会刷掉一部分
                owner_kind="memory",
            )
            for hit in hits:
                candidates[hit.owner_id] = hit.distance

        rows = list(await self._fetch_active(
            tenant_id=tenant_id, scope=scope, owner_id=owner_id, kinds=kinds,
            ids=list(candidates) if candidates else None,
            exclude_ids=exclude_ids, limit=top_k,
            order_by_rank=not candidates, now=moment,
        ))

        if candidates:
            order = {mid: index for index, mid in enumerate(candidates)}
            rows.sort(key=lambda row: order.get(row.memory_id, 1 << 30))

        return [(_to_view(row), candidates.get(row.memory_id)) for row in rows[:top_k]]

    async def _fetch_active(
        self,
        *,
        tenant_id: str,
        scope: str,
        owner_id: str,
        kinds: Sequence[str] | None,
        ids: Sequence[str] | None,
        exclude_ids: Sequence[str],
        limit: int,
        order_by_rank: bool,
        now: datetime,
    ) -> Sequence[MemoryRow]:
        conditions = [
            MemoryRow.state == "active",
            MemoryRow.tenant_id == tenant_id,
            MemoryRow.scope == scope,
            # 硬时效：过期的不算当前事实（`NFR-M-09`）。
            (MemoryRow.valid_to.is_(None)) | (MemoryRow.valid_to > now),
        ]
        if owner_id:
            # owner 为空表示「这一层不做 owner 划分」，此时不按它过滤。
            conditions.append((MemoryRow.owner_id == owner_id) | (MemoryRow.owner_id == ""))
        if kinds:
            conditions.append(MemoryRow.kind.in_(list(kinds)))
        if ids is not None:
            conditions.append(MemoryRow.memory_id.in_(list(ids)))
        if exclude_ids:
            conditions.append(MemoryRow.memory_id.notin_(list(exclude_ids)))

        statement = select(MemoryRow).where(*conditions)
        if order_by_rank:
            statement = statement.order_by(MemoryRow.weight.desc(), MemoryRow.updated_at.desc())
        return list(await self.tx.fetch_all(statement.limit(limit)))

    async def sweep_candidates(
        self,
        *,
        limit: int,
        offset: int = 0,
        tenant_id: str = "",
        scope: str = "",
        older_than: datetime | None = None,
        now: datetime | None = None,
    ) -> Sequence[MemoryView]:
        """离线衰减的扫描：取一批 ``active`` 记忆。

        按 ``last_accessed_at`` **升序**（最久没用的排前面）—— 衰减的任务是
        「找出该退场的」，先看最可能该退场的那些。
        """
        conditions = [MemoryRow.state == "active"]
        if tenant_id:
            conditions.append(MemoryRow.tenant_id == tenant_id)
        if scope:
            conditions.append(MemoryRow.scope == scope)
        if older_than is not None:
            conditions.append(
                (MemoryRow.last_accessed_at.is_(None)) | (MemoryRow.last_accessed_at < older_than)
            )
        rows = await self.tx.fetch_all(
            select(MemoryRow)
            .where(*conditions)
            .order_by(MemoryRow.last_accessed_at.asc().nulls_first(), MemoryRow.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return [_to_view(row) for row in rows]

    async def cluster_members(
        self, *, tenant_id: str = "", scope: str = "", kinds: Sequence[str], limit: int
    ) -> Sequence[MemoryView]:
        """取一批同作用域、同种类的 ``active`` 记忆，供蒸馏找冗余簇。

        ``tenant_id`` / ``scope`` 为空表示**不限**（取全部）。过滤只做在非空时 ——
        否则「按空串精确匹配」会把所有行都过滤掉，而调用方看到的是「没有冗余簇」，
        一个看起来完全正常的结论。
        """
        conditions = [MemoryRow.state == "active", MemoryRow.kind.in_(list(kinds))]
        if tenant_id:
            conditions.append(MemoryRow.tenant_id == tenant_id)
        if scope:
            conditions.append(MemoryRow.scope == scope)
        rows = await self.tx.fetch_all(
            select(MemoryRow)
            .where(*conditions)
            .order_by(MemoryRow.weight.desc(), MemoryRow.created_at.desc())
            .limit(limit)
        )
        return [_to_view(row) for row in rows]

    async def trajectory(
        self,
        *,
        tenant_id: str = "",
        scope: str = "",
        since: datetime | None = None,
        kinds: Sequence[str] | None = None,
        limit: int = 200,
        now: datetime | None = None,
    ) -> Sequence[MemoryView]:
        """取一段记忆轨迹（反思的输入，`FR-M-06`）。

        **有上界**：一次反思读取的条数必须封顶，否则一次长跑能把整库读进上下文。
        """
        conditions: list[Any] = [MemoryRow.state == "active"]
        if tenant_id:
            conditions.append(MemoryRow.tenant_id == tenant_id)
        if scope:
            conditions.append(MemoryRow.scope == scope)
        if kinds:
            conditions.append(MemoryRow.kind.in_(list(kinds)))
        if since is not None:
            conditions.append(MemoryRow.created_at >= since)
        rows = await self.tx.fetch_all(
            select(MemoryRow)
            .where(*conditions)
            .order_by(MemoryRow.created_at.desc())
            .limit(limit)
        )
        return [_to_view(row) for row in rows]

    async def last_maintenance_at(self) -> datetime | None:
        """上一次离线维护的时刻（游标的来源，`FR-M-18`）。

        维护任务用 ``decay`` / ``reflect`` 两类轨迹行的最新时刻作游标 ——
        这样**不需要第三张表**来存游标，而轨迹本身已经记录了一切。
        """
        result = await self.tx.scalar(
            select(MemoryOpRow.occurred_at)
            .where(MemoryOpRow.op.in_(["decay", "reflect"]))
            .order_by(MemoryOpRow.occurred_at.desc())
            .limit(1)
        )
        return _as_utc(result)

    async def _load(self, memory_id: str) -> MemoryRow | None:
        return await self.tx.fetch_one(
            select(MemoryRow).where(MemoryRow.memory_id == memory_id)
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _to_view(row: MemoryRow) -> MemoryView:
    return MemoryView(
        memory_id=row.memory_id,
        kind=row.kind,
        content=row.content,
        scope=row.scope,
        state=row.state,
        tenant_id=row.tenant_id,
        owner_id=row.owner_id,
        summary=row.summary,
        content_hash=row.content_hash,
        category=row.category,
        tags=tuple(row.tags or ()),
        sensitivity=row.sensitivity,
        source_kind=row.source_kind,
        source_session_id=row.source_session_id,
        source_agent=row.source_agent,
        source_trace_id=row.source_trace_id,
        session_id=row.session_id,
        caller=row.caller,
        confidence=row.confidence,
        importance=row.importance,
        weight=row.weight,
        version=row.version,
        schema_version=row.schema_version,
        supersedes_id=row.supersedes_id,
        superseded_by=row.superseded_by,
        merged_into=row.merged_into,
        access_count=row.access_count,
        used_count=row.used_count,
        evidence_count=row.evidence_count,
        valid_from=_as_utc(row.valid_from),
        valid_to=_as_utc(row.valid_to),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        last_accessed_at=_as_utc(row.last_accessed_at),
        last_decayed_at=_as_utc(row.last_decayed_at),
        metadata=dict(row.meta or {}),
    )


def _as_utc(moment: datetime | None) -> datetime | None:
    """把读回来的时间戳**补成 aware**。

    SQLite（单测）回来的 ``DateTime(timezone=True)`` 是 **naive** 的，
    而业务侧拿去和 aware 的 ``now`` 相减 / 比较会抛
    ``can't compare offset-naive and offset-aware datetimes`` ——
    报错本身不会告诉你「是数据库方言的问题」。

    与 ``repo/tool.py`` 的 ``find`` 是同一处坑，收在**读取的边界**上修一次，
    而不是让每个用时间的地方各自防御。
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

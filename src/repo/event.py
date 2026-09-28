"""事件落库（``FR-R-03``）—— **append-only**。

**为什么单独强调只增不改**：事件是「当时发生了什么」的记录。
给它加上 update / delete，就等于给了「事后修改历史」的能力 ——
而当你在排查一次故障时，最不能接受的就是「这条记录可能被改过」。
所以本仓储**不提供**任何修改或删除方法（不是靠约定，是没有那些方法）。

**与 ``src/event`` 的分工**：事件的**语义**（有哪些类型、字段什么意思、
怎么订阅分发）属于 ``src/event``；本文件只管「把它们可靠地存下来、按维度取出来」。

**gateway 不发事件到别处**（B-4）：所有事件都在门面产生，
所以这里的 ``EventRow`` 天然只承载一类来源。将来其它模块要发事件，
应当走 ``src/event`` 的统一出口，而不是各自往这张表里塞。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from foundation.db import Base, BigIntPk
from repo.base import Repository
from repo.types import BatchResult, TimeRange

__all__ = ["EventRepo", "EventRow"]

_log = logging.getLogger(__name__)


class EventRow(Base):
    """一条事件。**追加写，永不修改。**"""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    #: 事件标识。**由生产者给出而不是数据库生成** ——
    #: 这样同一批事件重复交付时可以用它做幂等（``ON CONFLICT DO NOTHING``），
    #: 而自增主键做不到这件事（重放会产生新的 id）。
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: 事件名，如 ``gateway.call.started``
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- 可关联标识（FR-G-10 要求事件必须能串成线）----------------------
    trace_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    caller: Mapped[str | None] = mapped_column(Text, nullable=True)
    alias: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 本次是第几次尝试（含首发）。**事件按它排序才能还原一次调用的过程**
    attempt_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 事件载荷。用 JSON 而不是一堆列：事件类型会持续增加，
    #: 每加一类就改表结构的话，迁移会变成高频动作。
    #: **代价**是查询能力弱 —— 需要按载荷内字段查时，应当把它提升成真正的列。
    payload: Mapped[Mapping[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # 排障的第一动作是按 trace_id 拉出一条完整的线
        Index("ix_event_trace", "trace_id", "occurred_at"),
        Index("ix_event_session", "session_id", "occurred_at"),
        Index("ix_event_name_occurred", "name", "occurred_at"),
        Index("ix_event_occurred", "occurred_at"),
    )

    def __repr__(self) -> str:
        return f"EventRow(name={self.name!r}, trace={self.trace_id!r})"


class EventRepo(Repository):
    """事件的仓储。**只有写与读，没有改与删。**"""

    async def append_many(self, rows: Sequence[EventRow]) -> BatchResult:
        """批量追加。

        **重复的 ``event_id`` 会被忽略，而不是报错**（幂等）。
        理由：交付方可能在「写成功但确认丢失」之后重试 ——
        那在分布式里是正常现象，不该让整批失败，也不该产生重复行。
        被忽略的条数计入 ``BatchResult.dropped``，**不能静默吞掉**。
        """
        items = list(rows)
        if not items:
            return BatchResult()

        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        # 两个后端各有自己的 upsert 构造器；按方言选。
        # **不用「先查再插」**：那在并发下会竞态，且多一次往返。
        dialect = self.tx.dialect_name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert

        # **用 RETURNING 而不是 rowcount**：批量执行（executemany）返回的是
        # ``IteratorResult``，它没有 ``rowcount`` —— 拿不到「实际插了几行」，
        # 也就分不清「写成功」与「因重复被忽略」。而这两件事必须能区分：
        # 前者是正常，后者说明同一批被交付了两次。
        statement = (
            insert(EventRow)
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(EventRow.event_id)
        )
        result = await self.tx.execute(
            statement,
            [
                {
                    "event_id": row.event_id,
                    "name": row.name,
                    "trace_id": row.trace_id,
                    "session_id": row.session_id,
                    "caller": row.caller,
                    "alias": row.alias,
                    "model_key": row.model_key,
                    "attempt_index": row.attempt_index,
                    "payload": dict(row.payload or {}),
                    "occurred_at": row.occurred_at,
                }
                for row in items
            ],
        )
        written = len(result.scalars().all())
        skipped = len(items) - written
        if skipped:
            _log.warning(
                "有 %d/%d 条事件因 event_id 重复被忽略（幂等重放）。"
                "若这个数字在意料之外，说明同一批事件被交付了两次。",
                skipped,
                len(items),
            )
        return BatchResult(written=written, dropped=skipped)

    # ---------------------------------------------------------------- 读
    async def by_trace(self, trace_id: str) -> Sequence[EventRow]:
        """按 trace_id 取一条完整的线，**按发生顺序**。

        排序不能只靠 ``occurred_at``：同一批交付的事件时间戳是**被拉平的**
        （见 ``composition.event_sink`` 关于 occurred_at 的说明），
        所以要再用自增 id 兜底 —— 否则「重试前还是重试后」会看不出来。
        """
        return list(
            await self.tx.fetch_all(
                select(EventRow)
                .where(EventRow.trace_id == trace_id)
                .order_by(EventRow.occurred_at, EventRow.id)
            )
        )

    async def by_session(
        self, session_id: str, *, window: TimeRange | None = None, limit: int = 500
    ) -> Sequence[EventRow]:
        statement = select(EventRow).where(EventRow.session_id == session_id)
        if window is not None:
            if window.start is not None:
                statement = statement.where(EventRow.occurred_at >= window.start)
            if window.end is not None:
                statement = statement.where(EventRow.occurred_at < window.end)
        return list(
            await self.tx.fetch_all(
                statement.order_by(EventRow.occurred_at.desc(), EventRow.id.desc()).limit(limit)
            )
        )

    async def count_by_name(self, *, window: TimeRange | None = None) -> dict[str, int]:
        """按事件名计数 —— 「重试了多少次、降级了多少次」这类问题的最小答案。"""
        from sqlalchemy import func as sql_func

        statement = select(EventRow.name, sql_func.count()).group_by(EventRow.name)
        if window is not None and window.start is not None:
            statement = statement.where(EventRow.occurred_at >= window.start)
        if window is not None and window.end is not None:
            statement = statement.where(EventRow.occurred_at < window.end)

        rows = await self.tx.execute(statement)
        return {name: int(count) for name, count in rows.all()}

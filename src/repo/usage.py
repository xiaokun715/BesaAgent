"""用量与成本的落库（``FR-R-07``）—— 本模块**第一优先级**。

**它接上的是一根已经断掉的线。** ``src/gateway/usage.py`` 的 ``UsageLedger.drain()``
写着「交付给 ``src/repo`` 的唯一入口」，但在此文件出现之前，全仓库**零个调用方**：
用量记在内存里，然后随着进程一起消失。三个具体的断口：

1. ``drain()`` 没人调 → 记录不会离开内存；
2. ``dropped``（丢弃计数）**全仓库零处读取** → 丢了数据这件事没有任何出口；
3. 失败记录的 ``alias`` 是空串（``gateway.py:764``）→ 按逻辑名聚合会静默丢掉它们。

**本模块只负责第 1、2 条的落库侧**；第 3 条的修法在 gateway（见 ``record_many`` 的说明）。

**依赖方向**：本文件**不 import ``src/gateway``**（``NFR-R-03``）。
也就是说 ``UsageRow`` 不是 ``UsageRecord`` 的子类或包装 ——
两者的映射由**组合根**完成（只有它能同时看见两边）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from foundation.db import Base, BigIntPk
from repo.base import Repository
from repo.types import BatchResult

__all__ = ["UsageDropRow", "UsageRepo", "UsageRow", "usage_amount_of"]

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 表
# --------------------------------------------------------------------------- #


class UsageRow(Base):
    """一次模型调用的用量与成本。

    **三个 token 列都可为 ``NULL``**，而且这是**刻意的**：
    ``NULL`` 表示「上游没说」，``0`` 表示「确实用了 0 个」。
    混同会让成本报表静默失真（``NFR-R-08`` / ``FR-G-08``）。

    这个不变量**无法用 DDL 强制** —— 没有哪种约束能表达「不许写 0」。
    所以它靠代码纪律守，并由测试 ``test_unknown_usage_is_stored_as_null`` 钉住。
    """

    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    # ---- 归属 ----------------------------------------------------------
    trace_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 逻辑名。**失败记录也必须非空** —— 为空会让按逻辑名的聚合静默丢掉它们
    alias: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # ---- 用量（NULL = 未知，不是 0）-------------------------------------
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- 成本 ----------------------------------------------------------
    #: ``numeric(18,8)``：与 ``gateway.cost`` 的 ``Decimal`` 除法对齐。
    #: 不用 float —— 它在数千次累加后会漂移，而账单要求「对得上」。
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="", nullable=False)

    # ---- 解释性字段 -----------------------------------------------------
    #: 是否发生降级 —— 「这次特别贵」往往就是因为降级到了强模型
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 本次是第几次尝试（含首发）。重试会重复计费，这个字段让账单可解释
    attempt_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # ---- 聚合维度 -------------------------------------------------------
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    caller: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: **绝对时刻**，由交付方在落库时补。
    #: ``UsageRecord.mono_at`` 是单调时钟读数，**跨进程不可比**，不能直接入库。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_usage_session_occurred", "session_id", "occurred_at"),
        Index("ix_usage_model_occurred", "model_key", "occurred_at"),
        Index("ix_usage_alias_occurred", "alias", "occurred_at"),
    )

    def __repr__(self) -> str:
        return (
            f"UsageRow(trace={self.trace_id!r}, alias={self.alias!r}, "
            f"model={self.model_key!r}, in={self.input_tokens}, out={self.output_tokens})"
        )


class UsageDropRow(Base):
    """内存账本溢出时被丢弃的记录数（``FR-R-07`` 的「``dropped`` 必须有出口」）。

    **为什么值得为它单开一张表**：gateway 的 ``UsageLedger.dropped`` 记录了丢弃数，
    但全仓库没有一处读它 —— 于是「账单少了几笔」这件事**没有任何出口**，
    报表上看起来一切正常。

    它不是日志就能替代的：日志会被轮转掉，而「上个月到底丢了多少」需要能查。
    """

    __tablename__ = "usage_drops"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    dropped_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 丢弃原因。目前只有 ``ledger_overflow``（内存账本超过 ``max_records``）
    reason: Mapped[str] = mapped_column(Text, default="ledger_overflow", nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"UsageDropRow(count={self.dropped_count}, reason={self.reason!r})"


def usage_amount_of(rows: Sequence[UsageRow], *, model_key: str | None = None) -> Decimal | None:
    """按行汇总成本。**任一行的成本未知则整体返回 ``None``**。

    与 ``gateway.cost`` 的语义一致：不知道就是不知道，不拿已知的那部分冒充当次总额。
    （gateway 的 ``cost_by_model`` 用的是另一种口径 —— 它把未知的模型单独保留，
    因为它回答的是「每个模型各花了多少」；本函数回答的是「这一批总额是多少」。）
    """
    total: Decimal | None = Decimal(0)
    seen = False
    for row in rows:
        if model_key is not None and row.model_key != model_key:
            continue
        seen = True
        if row.cost_amount is None:
            return None
        total = (total or Decimal(0)) + row.cost_amount
    return total if seen else None


# --------------------------------------------------------------------------- #
# 仓储
# --------------------------------------------------------------------------- #


class UsageRepo(Repository):
    """用量与成本的仓储。"""

    async def record_many(self, rows: Sequence[UsageRow]) -> BatchResult:
        """批量落库。**一次事务多行**（``NFR-R-06``）。

        ``alias`` 为空的行**照存不误，但会告警**。取舍说明：
        丢掉它们会让账单真的少一块（数据不可恢复），存下来只是聚合时漏掉 ——
        两害相权，**保住数据**。
        """
        items = list(rows)
        if not items:
            return BatchResult()

        blank_alias = sum(1 for row in items if not row.alias)
        if blank_alias:
            # 这条告警对应 gateway 的已知缺陷：_record_failure_usage 把 alias 写成了空串。
            # 记录在这里是为了「即使没人看告警，也能从库里查出来」。
            _log.warning(
                "有 %d/%d 条用量记录的逻辑名（alias）为空 —— 按逻辑名的聚合会漏掉它们。"
                "该字段应由 gateway 的 _record_failure_usage 填入。",
                blank_alias,
                len(items),
            )

        self.tx.add_all(items)
        await self.tx.flush()
        return BatchResult(written=len(items))

    async def record_dropped(self, dropped: int, *, reason: str = "ledger_overflow") -> None:
        """记录被丢弃的条数。**只在 ``dropped > 0`` 时调用** —— 见 ``record_ledger``。"""
        if dropped <= 0:
            return
        self.tx.add(UsageDropRow(dropped_count=int(dropped), reason=reason))
        await self.tx.flush()

    async def record_ledger(
        self, rows: Sequence[UsageRow], *, dropped: int = 0
    ) -> BatchResult:
        """把「一批记录 + 丢弃数」作为一次交付落库。

        这是交付方应当调用的方法：**记录与丢弃数在同一个事务里** ——
        否则会出现「记录写了、丢弃数没写」的窗口，
        而那个窗口里丢掉的数据永远不会被任何人发现。
        """
        result = await self.record_many(rows)
        if dropped > 0:
            await self.record_dropped(dropped)
            _log.warning(
                "用量账本丢弃了 %d 条记录（内存上限溢出）。"
                "**这个数字大于 0 就是缺陷** —— 它意味着账单会少几笔。",
                dropped,
            )
        return result

    # ---------------------------------------------------------------- 读
    async def list_by_session(self, session_id: str, *, limit: int = 100) -> Sequence[UsageRow]:
        from sqlalchemy import select

        return list(
            await self.tx.fetch_all(
                select(UsageRow)
                .where(UsageRow.session_id == session_id)
                .order_by(UsageRow.occurred_at.desc())
                .limit(limit)
            )
        )

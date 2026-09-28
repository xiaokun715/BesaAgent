"""工具执行的**权威记录**（``FR-T-07`` / ``FR-T-08``）。

**为什么它在这里而不是 ``src/tool/`` 自持**（架构文档 ``DT-9``）：
它是**基础设施数据** —— 谁在什么时候执行了什么、结果如何 ——
与 ``event`` / ``usage`` 同类，不是某个测试阶段的领域产物。
``FR-R-09`` 的「业务模块自持表」是为「测试用例 / 缺陷」那类东西设的。

**为什么它不能只存在 Redis 里**：Redis 在本项目的定位是「**能丢的才进 Redis**」。
而「这个调用执行过」是**不能丢的** —— 丢了不是「查不到」，而是
**静默地变成「可以重复执行」**。那正是这个模块存在的理由。

**``state`` 有四档而不是两档**：``in_flight`` / ``done`` / ``failed`` / ``uncertain``。
``uncertain`` 表示「登记了开始、没等到结束」——
工具层没有能力判断副作用到底发生没有，所以它不猜（``FR-T-08``）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from foundation.db import Base, BigIntPk
from repo.base import Repository

__all__ = ["EXEC_STATES", "ExecutionView", "ToolExecutionRepo", "ToolExecutionRow"]

_log = logging.getLogger(__name__)

#: 执行状态。``uncertain`` 是**读取时**判定出来的（见 :meth:`ToolExecutionRepo.find`），
#: 不写回数据库 —— 写回是破坏性的（改完就回不去了），而读取时判定无状态且幂等。
EXEC_STATES: tuple[str, ...] = ("in_flight", "done", "failed", "uncertain")


@dataclass(frozen=True)
class ExecutionView:
    """一条执行记录的**只读视图**。

    **为什么 ``find`` 返回它而不是 ORM 行**：``find`` 会在读取时把超租约的
    ``in_flight`` 判成 ``uncertain`` —— 如果直接改 ORM 对象的属性，
    那个对象就变**脏**了，而 ORM 的脏跟踪会在下一次 flush/commit 时
    **真的把它写回数据库**。也就是说，「读的时候顺手改一下状态」这个写法
    会静默地把「不写回」变成「写回」，而两者的语义差别正是这一处的全部设计意图
    （见 :meth:`ToolExecutionRepo.find` 的 docstring）。

    返回一个不可变的视图，让「不写回」在**结构上**成立，而不是靠注释。
    """

    scope: str
    idem_key: str
    tool_name: str = ""
    state: str = "in_flight"
    owner: str = ""
    result: str | None = None
    result_truncated: bool = False
    error: str | None = None
    output_bytes: int = 0

    @property
    def done(self) -> bool:
        return self.state == "done"

    @property
    def uncertain(self) -> bool:
        return self.state == "uncertain"

    @property
    def retryable(self) -> bool:
        """这个状态下可以再试一次吗。

        ``failed`` 可以（上次真的失败了）；``in_flight`` / ``uncertain`` **不行** ——
        前者有人在跑，后者不知道副作用发生没有。后两者的重试是**上层的决定**，
        工具层不替它赌（``DT-8``）。
        """
        return self.state == "failed"


class ToolExecutionRow(Base):
    """一次工具执行的记录。**它同时是幂等记录、审计记录与排障依据。**"""

    __tablename__ = "tool_execution"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    #: 作用域 + 幂等键 = 唯一约束。**这是最后一道保险**：
    #: 两个进程同时登记时数据库只让一个成功，另一个拿到 ``IntegrityError`` ——
    #: 比「先查再插」可靠，后者在并发下的竞态窗口**不会报错**。
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    idem_key: Mapped[str] = mapped_column(Text, nullable=False)

    tool_name: Mapped[str] = mapped_column(Text, default="", nullable=False)
    side_effect: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    #: 参数指纹。**与 ``idem_key`` 不同**：键可能来自调用方的显式指定，
    #: 而指纹永远由参数算出 —— 两者并存才好在「参数一样但键不同」时排查
    args_digest: Mapped[str] = mapped_column(Text, default="", nullable=False)

    state: Mapped[str] = mapped_column(String(16), default="in_flight", nullable=False)
    #: 登记的持有者。用来判断「这条 in_flight 是不是我留的」
    owner: Mapped[str] = mapped_column(Text, default="", nullable=False)

    trace_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    caller: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: 结果载荷。有大小上限（超限只留摘要并把 ``result_truncated`` 置真）——
    #: 一个 ``bash`` 的输出可以是几百 MB，不能无脑塞进权威表。
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    #: 失败原因（已脱敏的字符串）。**不存异常对象** —— 与 ``AttemptRecord`` 同一条理由
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 输出字节数。即便结果被截断，这个数字也保留 ——
    #: 「它到底产出了多少」是有用的事实，不该因为存不下就一起丢掉
    output_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", "idem_key", name="uq_tool_execution_scope_idem"),
        Index("ix_tool_execution_trace", "trace_id"),
        Index("ix_tool_execution_session_started", "session_id", "started_at"),
        Index("ix_tool_execution_state_started", "state", "started_at"),
    )

    def __repr__(self) -> str:
        return (
            f"ToolExecutionRow(tool={self.tool_name!r}, state={self.state!r}, "
            f"key={self.idem_key[:12]!r})"
        )


class ToolExecutionRepo(Repository):
    """工具执行记录的仓储。

    **写入时机由 ``executor`` 保证**：登记（``register``）**必须先于执行** ——
    反过来的话，崩在「执行完但没登记」之间，下次重试会**再执行一遍**，
    而系统完全不知道发生过。
    """

    # ---------------------------------------------------------------- 写
    async def register(
        self,
        *,
        scope: str,
        idem_key: str,
        tool_name: str,
        side_effect: str,
        args_digest: str,
        owner: str,
        trace_id: str = "",
        session_id: str | None = None,
        caller: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """登记「开始执行」。

        Returns:
            ``True`` 登记成功（可以执行）；``False`` 已被别人登记（**不要执行**）。

        唯一约束冲突被翻译成 ``False`` 而不是异常 —— 并发下这是**正常现象**，
        不是错误。把异常抛出去会让调用方以为「登记失败」，从而去重试 —— 正好做反。
        """
        row = ToolExecutionRow(
            scope=scope,
            idem_key=idem_key,
            tool_name=tool_name,
            side_effect=side_effect,
            args_digest=args_digest,
            state="in_flight",
            owner=owner,
            trace_id=trace_id,
            session_id=session_id,
            caller=caller,
            started_at=now or datetime.now(timezone.utc),
        )
        try:
            self.tx.add(row)
            await self.tx.flush()
        except IntegrityError:
            # 唯一约束把它挡住了 —— 有人比我们先到。**不是错误**。
            _log.debug("工具执行登记冲突（幂等生效）：tool=%s key=%s", tool_name, idem_key)
            return False
        return True

    async def mark_done(
        self,
        *,
        scope: str,
        idem_key: str,
        result: str | None,
        truncated: bool,
        output_bytes: int,
        now: datetime | None = None,
    ) -> None:
        """登记「执行完成」。"""
        await self._finish(
            scope=scope,
            idem_key=idem_key,
            state="done",
            result=result,
            truncated=truncated,
            output_bytes=output_bytes,
            error=None,
            now=now,
        )

    async def mark_failed(
        self,
        *,
        scope: str,
        idem_key: str,
        error: str,
        now: datetime | None = None,
    ) -> None:
        """登记「执行失败」。

        **失败不是终态**：上层可以决定重试，而重试要能重新登记。
        所以 ``mark_failed`` 之后那条记录的状态是 ``failed``——
        它既不阻止重试（不会让第二个调用被当成 in_flight 拒掉），
        也不假装成功。
        """
        await self._finish(
            scope=scope,
            idem_key=idem_key,
            state="failed",
            result=None,
            truncated=False,
            output_bytes=0,
            error=error,
            now=now,
        )

    async def reopen(
        self,
        *,
        scope: str,
        idem_key: str,
        owner: str,
        now: datetime | None = None,
    ) -> bool:
        """把一条 ``failed`` 记录重新打开成 ``in_flight``。

        **为什么需要它**：``(scope, idem_key)`` 上有唯一约束，所以「失败后重试」
        不能靠再插一条 —— 那会被约束挡住，而挡住的表现是 ``register`` 返回 ``False``，
        也就是**把这个键当成「有人在跑」永久锁死**。
        一次偶发故障就会让那个键再也跑不了，而且没有任何报错。

        Returns:
            是否成功重开（记录不存在或状态不是 ``failed`` 时返回 ``False``）。
        """
        row = await self._load(scope, idem_key)
        if row is None or row.state != "failed":
            return False
        row.state = "in_flight"
        row.owner = owner
        row.error = None
        row.result = None
        row.result_truncated = False
        row.finished_at = None
        row.started_at = now or datetime.now(timezone.utc)
        await self.tx.flush()
        return True

    async def _finish(
        self,
        *,
        scope: str,
        idem_key: str,
        state: str,
        result: str | None,
        truncated: bool,
        output_bytes: int,
        error: str | None,
        now: datetime | None,
    ) -> None:
        row = await self._load(scope, idem_key)
        if row is None:
            # 理论上不该发生（登记先于执行）。真发生了说明有人绕过 executor——
            # 静默忽略会让那种绕过永远查不出来，所以出声。
            _log.warning(
                "要更新一条不存在的工具执行记录：tool_key=%s state=%s。"
                "这通常意味着有人绕过了 executor 的执行流程。",
                idem_key,
                state,
            )
            return
        row.state = state
        row.result = result
        row.result_truncated = truncated
        row.output_bytes = output_bytes
        row.error = error
        row.finished_at = now or datetime.now(timezone.utc)
        await self.tx.flush()

    # ---------------------------------------------------------------- 读
    async def find(
        self,
        scope: str,
        idem_key: str,
        *,
        lease_s: float = 120.0,
        now: datetime | None = None,
    ) -> ExecutionView | None:
        """按作用域 + 键取记录，**返回只读视图**。

        **``uncertain`` 在这里判定**：登记了 ``in_flight`` 但超过租约还没结束，
        说明持有者多半崩了 —— 但「它崩在执行前还是执行后」**不知道**。
        所以状态是 ``uncertain``，既不是成功也不是失败（``FR-T-08``）。

        判定放在读取时而不是后台扫描：后台任务要处理「它自己崩了」，
        且改状态是**破坏性**的（``in_flight`` 改成 ``uncertain`` 之后就回不去了）；
        而读取时判定无状态、幂等，且迟到的 ``done`` 仍然能覆盖它。

        **返回视图而不是 ORM 行**，「不写回」才是结构上成立的 ——
        直接改 ORM 对象的属性会让它变脏，而脏跟踪会在下一次提交时真的落库。
        """
        row = await self._load(scope, idem_key)
        if row is None:
            return None

        state = row.state
        if state == "in_flight":
            current = now or datetime.now(timezone.utc)
            started = row.started_at
            if started.tzinfo is None:  # SQLite 回来的可能是 naive
                started = started.replace(tzinfo=timezone.utc)
            if current - started > timedelta(seconds=lease_s):
                state = "uncertain"

        return ExecutionView(
            scope=row.scope,
            idem_key=row.idem_key,
            tool_name=row.tool_name,
            state=state,
            owner=row.owner,
            result=row.result,
            result_truncated=row.result_truncated,
            error=row.error,
            output_bytes=row.output_bytes,
        )

    async def _load(self, scope: str, idem_key: str) -> ToolExecutionRow | None:
        return await self.tx.fetch_one(
            select(ToolExecutionRow).where(
                ToolExecutionRow.scope == scope, ToolExecutionRow.idem_key == idem_key
            )
        )

    async def by_trace(self, trace_id: str) -> Sequence[ToolExecutionRow]:
        return list(
            await self.tx.fetch_all(
                select(ToolExecutionRow)
                .where(ToolExecutionRow.trace_id == trace_id)
                .order_by(ToolExecutionRow.started_at, ToolExecutionRow.id)
            )
        )

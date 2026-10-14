"""断点与续跑（``FR-R-05``）。

**它回答的问题是「这次运行到哪了」** —— 不是「这次调用做了什么」。
后者是 ``tool_execution`` 与 ``event`` 的事。

## ⚠ 与工具层的关系：检查点由 **agent** 写，不由工具写

原设计的措辞是「工具执行后写检查点」，实现时发现那有两处不对：

1. **它和 ``tool_execution`` 高度重叠** —— 每次调用一条检查点，等于把同一件事
   存两遍，而两份会漂移；
2. **工具不知道自己在一个什么样的运行里**。它只拿到 ``scope``；
   而「plan 的第 3 步做了什么、还剩几步」只有 agent 知道。

所以：**工具报告「这一次调用」，agent 报告「这一步/这次运行」**。
这与「谁拥有这个信息，谁记录它」是同一条纪律。

## ``format_version`` 是这个模块存在的理由之一

快照的 ``state`` 是 JSON，而 JSON **没有 schema**。所以：

- 写的时候带上 ``format_version``；
- 读的时候**版本不认识就报错**，不得按旧格式猜。

猜的代价是：一个字段在新版里语义变了（比如 ``done`` 从 bool 变成列表），
旧代码把它当 bool 读 —— 得到的是「真值」而不是异常，于是一次续跑
悄悄地从错误的地方开始。那比读不出来严重得多。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from foundation.db import Base, BigIntPk
from repo.base import Repository

__all__ = [
    "CURRENT_FORMAT_VERSION",
    "FINISHED_STATUSES",
    "NEEDS_REVIEW_STATUS",
    "UNFINISHED_STATUSES",
    "CheckpointRepo",
    "CheckpointRow",
    "CheckpointView",
    "UnknownCheckpointVersion",
]

_log = logging.getLogger(__name__)

#: 当前快照格式的版本。**改 ``state`` 的字段语义时必须 +1**，否则旧代码会静默读错。
CURRENT_FORMAT_VERSION = 1

#: 需要**人看一眼**的状态（`docs/agent` 的 `FR-A-04`）：上一次运行停在了一个
#: 「工具执行过、但结果没回填」的位置，所以「接着跑还是重来」不能由程序替人决定。
NEEDS_REVIEW_STATUS = "needs_review"

#: 已经收尾的状态。**判定"能不能续跑"看它**。
FINISHED_STATUSES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

#: 还活着、需要被"未完成的运行"列出来的状态。
#:
#: ⚠ **必须含 ``needs_review``**：只查 ``running`` 的话，
#: **最需要人看的那一类运行恰恰不会出现在「未完成的运行」里** ——
#: 于是它会一直卡着，而唯一的线索也不在列表上。
UNFINISHED_STATUSES: tuple[str, ...] = ("running", NEEDS_REVIEW_STATUS)


class UnknownCheckpointVersion(RuntimeError):
    """快照的格式版本不认识。

    **必须硬失败** —— 见模块 docstring：按旧格式猜的代价是「悄悄从错误的地方开始」。
    """

    def __init__(self, run_id: str, version: int) -> None:
        self.run_id = run_id
        self.version = version
        super().__init__(
            f"运行 {run_id!r} 的快照格式版本是 {version}，"
            f"而当前代码只认到 {CURRENT_FORMAT_VERSION}。\n"
            "**不要按旧格式猜着读** —— 字段语义可能已经变了，"
            "猜的结果是一次「看起来正常、但从错误的地方继续」的续跑。\n"
            "要恢复它，请用写出这份快照的那个版本，或写一条迁移把它转过来。"
        )


@dataclass(frozen=True)
class CheckpointView:
    """只读视图 —— 与 ``repo.tool`` 里那个同样的理由：读取时可能要做判定，
    而**改 ORM 对象的属性会让它变脏**，脏跟踪会在下一次提交时真的写回去。"""

    run_id: str
    step_index: int = 0
    node_id: str = ""
    status: str = "running"
    state: Mapping[str, Any] = None  # type: ignore[assignment]
    artifacts: Mapping[str, Any] = None  # type: ignore[assignment]
    format_version: int = CURRENT_FORMAT_VERSION
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state is None:
            object.__setattr__(self, "state", {})
        if self.artifacts is None:
            object.__setattr__(self, "artifacts", {})

    @property
    def finished(self) -> bool:
        """已经收尾了吗。

        ``needs_review`` **不在**这里 —— 它没跑完，只是停下来了
        （详见 :data:`FINISHED_STATUSES` 与 :data:`NEEDS_REVIEW_STATUS`）。
        """
        return self.status in FINISHED_STATUSES


class CheckpointRow(Base):
    """一次运行的快照。**追加写，不更新**（历史本身就是有用的信息）。"""

    __tablename__ = "checkpoint"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)

    #: 运行标识。由 agent 生成（一次「跑一轮回归」、一次多智能体协作）
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 快照格式版本。**读的时候必须校验** —— 见模块 docstring
    format_version: Mapped[int] = mapped_column(
        Integer, default=CURRENT_FORMAT_VERSION, nullable=False
    )

    #: 第几步 / 哪个节点 / 什么状态
    step_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)

    #: 状态与产物。**存摘要不存全文** —— 它是状态不是日志；
    #: 全文在 ``tool_execution`` 与（大结果的）落盘文件里。
    #:
    #: 列类型必须**显式写 ``JSON``**：``Mapped[Mapping[str, Any]]`` 这个注解
    #: SQLAlchemy 推不出该用哪种列类型（它会尝试把 ``Mapping`` 当 Python 类型解析，
    #: 然后报 ``MappedAnnotationError``）。``repo/event.py`` 里同样显式写了。
    state: Mapped[Mapping[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    artifacts: Mapped[Mapping[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    caller: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=None
    )

    __table_args__ = (
        # 取「最近一次快照」是最高频的操作（续跑时第一件事就是它）
        Index("ix_checkpoint_run_created", "run_id", "created_at"),
        Index("ix_checkpoint_status", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"CheckpointRow(run={self.run_id!r}, step={self.step_index}, "
            f"status={self.status!r})"
        )


class CheckpointRepo(Repository):
    """快照的读写。**只追加，不更新**。"""

    async def save(
        self,
        run_id: str,
        *,
        step_index: int = 0,
        node_id: str = "",
        status: str = "running",
        state: Mapping[str, Any] | None = None,
        artifacts: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        caller: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """写一条快照。

        **不更新旧的那条**：历史本身就是信息 ——
        「上一步是什么」在排障时经常比「现在是什么」更有用，
        而更新会把它抹掉。代价是行数增长，靠 ``run_id`` 索引与清理解决。
        """
        self.tx.add(
            CheckpointRow(
                run_id=run_id,
                format_version=CURRENT_FORMAT_VERSION,
                step_index=int(step_index),
                node_id=node_id,
                status=status,
                state=dict(state or {}),
                artifacts=dict(artifacts or {}),
                session_id=session_id,
                caller=caller,
                created_at=now or datetime.now(timezone.utc),
            )
        )
        await self.tx.flush()

    async def latest(self, run_id: str) -> CheckpointView | None:
        """取最近一次快照。

        Raises:
            UnknownCheckpointVersion: 版本不认识。**不返回 ``None``** ——
                「没有快照」与「快照读不懂」是两件事，混同会让调用方
                把一次版本不兼容当成「从头开始跑」。
        """
        row = await self.tx.fetch_one(
            select(CheckpointRow)
            .where(CheckpointRow.run_id == run_id)
            .order_by(CheckpointRow.created_at.desc(), CheckpointRow.id.desc())
        )
        if row is None:
            return None

        if row.format_version > CURRENT_FORMAT_VERSION:
            raise UnknownCheckpointVersion(run_id, row.format_version)

        return CheckpointView(
            run_id=row.run_id,
            step_index=row.step_index,
            node_id=row.node_id,
            status=row.status,
            state=dict(row.state or {}),
            artifacts=dict(row.artifacts or {}),
            format_version=row.format_version,
            created_at=row.created_at,
        )

    async def history(self, run_id: str, *, limit: int = 200) -> Sequence[CheckpointView]:
        """一条运行的全部快照，**按时间正序** —— 还原过程要看顺序。"""
        rows = await self.tx.fetch_all(
            select(CheckpointRow)
            .where(CheckpointRow.run_id == run_id)
            .order_by(CheckpointRow.created_at, CheckpointRow.id)
            .limit(limit)
        )
        return [
            CheckpointView(
                run_id=row.run_id,
                step_index=row.step_index,
                node_id=row.node_id,
                status=row.status,
                state=dict(row.state or {}),
                artifacts=dict(row.artifacts or {}),
                format_version=row.format_version,
                created_at=row.created_at,
            )
            for row in rows
        ]

    async def unfinished(self) -> Sequence[CheckpointView]:
        """没有正常收尾的运行 —— 续跑要看的正是这些。

        **只看状态，不看时间**：一个「跑了三天还没结束」的运行
        和一个「正常结束」的运行，靠时间分不开。

        含 ``needs_review``（见 :data:`UNFINISHED_STATUSES`）——
        它是 agent 层在续跑时发现「有工具执行结果不明」而停下的状态，
        **最需要人看的就是它**。
        """
        rows = await self.tx.fetch_all(
            select(CheckpointRow)
            .where(CheckpointRow.status.in_(list(UNFINISHED_STATUSES)))
            .order_by(CheckpointRow.created_at.desc())
        )
        return [
            CheckpointView(
                run_id=row.run_id,
                step_index=row.step_index,
                node_id=row.node_id,
                status=row.status,
                state=dict(row.state or {}),
                artifacts=dict(row.artifacts or {}),
            )
            for row in rows
        ]

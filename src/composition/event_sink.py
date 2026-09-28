"""把 gateway 发的事件收下来、攒起来、再落库。

**为什么是「攒起来」而不是「收到就写」**：

1. gateway 的 ``_emit`` 明确要求「**绝不让观测影响主流程**」（``FR-G-10``）——
   在 emit 里直接开事务写库，会把数据库的延迟和故障带进模型调用路径；
2. 一次调用会产生好几条事件，逐条写就是逐条往返；
3. 最关键的：``foundation/database.py`` 的设计目标里写着
   「**checkpoint 落库 + 事件落库 + 用量落库**三步，若各是一个事务，
   崩在中间会留下互相矛盾的记录」—— 攒起来才能和用量**同一个事务**提交。

**``trace_id`` 从哪来（这里有个好用的现成机制）**：
gateway 的事件载荷里**只有三个位置带 ``trace_id``**，其余不带。
但 ``foundation/logging.py`` 已经用 ``contextvars`` 承载了当前 trace_id
（日志格式串要用它）—— 于是这里直接 :func:`current_trace_id` 取，
**不需要改 gateway**。这也是它必须是 contextvar 而不是全局变量的原因：
多 agent 并发时，全局变量会让 A 调用的事件挂上 B 的 trace_id。

**已知缺口：``session_id`` / ``caller`` 拿不到。**
gateway 的 7 个 emit 点全都不带这两个字段（本次架构分析记录在案）。
它们在本表里是**可空列**，所以事件照样能存，只是「按会话查事件」暂时查不出东西。
补它需要改 gateway（每个 emit 点都带上，或引入一次调用上下文的绑定），
属于跨模块改动，**没有和本次一起做** —— 见 ``docs/repo/架构概要设计-repo.md``
的实现期修订表。不要因为「表建好了」就以为这一维已经可用。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from foundation.database import Transaction
from foundation.logging import current_trace_id
from repo.event import EventRepo, EventRow
from repo.types import BatchResult

__all__ = ["BufferingEmitter", "flush_events", "to_event_rows"]

_log = logging.getLogger(__name__)

#: 缓冲上限。与 ``UsageLedger.max_records`` 同样的理由：
#: 无界缓冲会把「落库变慢」升级成「进程 OOM」。
#: 事件比用量条数多（一次调用 3~5 条），所以上限给得比用量大一些。
DEFAULT_MAX_BUFFERED = 20_000


class BufferingEmitter:
    """把事件攒在内存里，等交付方来取。

    形状上满足 gateway 的 ``EventEmitter`` 协议（一个 ``emit(name, payload)``），
    所以它能直接替换 ``NullEmitter`` 而不需要 gateway 知道任何事。
    """

    __slots__ = ("_max", "_pending", "_dropped")

    def __init__(self, *, max_records: int = DEFAULT_MAX_BUFFERED) -> None:
        if max_records < 1:
            raise ValueError("max_records 必须 >= 1")
        self._max = max_records
        self._pending: list[tuple[str, Mapping[str, Any]]] = []
        self._dropped = 0

    # ---------------------------------------------------------------- 收
    def emit(self, name: str, payload: Mapping[str, Any]) -> None:
        """收下一条事件。

        **这个方法必须永不抛异常**：gateway 的 ``_emit`` 已经用 ``except Exception``
        兜了一层，但那层只保证「调用不被打断」，失败原因会静默丢掉（只打 debug 日志）。
        在这里保持简单、不做任何 IO，是更稳的做法。
        """
        self._pending.append((name, payload))
        if len(self._pending) > self._max:
            overflow = len(self._pending) - self._max
            del self._pending[:overflow]
            self._dropped += overflow

    # ---------------------------------------------------------------- 取
    def drain(self) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """取出并清空。"""
        drained = tuple(self._pending)
        self._pending.clear()
        return drained

    def drain_dropped(self) -> int:
        """取出并清零丢弃计数（与 ``UsageLedger.drain_dropped`` 同构）。

        同样地：**只读不清会让同一笔丢弃被反复上报**。
        """
        dropped = self._dropped
        self._dropped = 0
        return dropped

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def dropped(self) -> int:
        return self._dropped


def to_event_rows(
    events: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    occurred_at: datetime,
) -> list[EventRow]:
    """把攒下的事件映射成行。

    ``event_id`` 在这里生成（uuid4），作用是**幂等**：同一批事件若被交付两次，
    第二次会因为 ``event_id`` 冲突而被忽略，而不是产生重复行。
    """
    rows: list[EventRow] = []
    # 日志模块的占位符是 "-"，那是「还没设置」的显示值，不是 trace_id。
    # 直接存进去会让「按 trace_id 查」多出一个叫 "-" 的垃圾桶。
    trace_id = current_trace_id()
    if trace_id == "-":
        trace_id = ""

    for name, payload in events:
        # 载荷里自己带了就以它为准 —— 它比上下文更具体
        # （例如一次调用里嵌了另一次调用时，上下文是外层的）。
        payload_trace = str(payload.get("trace_id") or "")
        rows.append(
            EventRow(
                event_id=uuid.uuid4().hex,
                name=name,
                trace_id=payload_trace or trace_id,
                # 这两个字段 gateway 目前不提供，留空是**如实**而不是遗漏。
                session_id=payload.get("session_id"),  # type: ignore[arg-type]
                caller=payload.get("caller"),  # type: ignore[arg-type]
                alias=str(payload.get("alias") or ""),
                model_key=str(payload.get("model") or payload.get("model_key") or ""),
                attempt_index=int(payload.get("attempt_index") or 0),
                payload=dict(payload),
                occurred_at=occurred_at,
            )
        )
    return rows


async def flush_events(
    tx: Transaction,
    emitter: BufferingEmitter,
    *,
    now: datetime | None = None,
) -> BatchResult:
    """把缓冲里的事件写进**调用方给的事务**。

    **刻意收 ``tx`` 而不是 ``Database``**：这样它就能和用量写在同一个事务里 ——
    而「三步同事务」正是 ``foundation/database.py`` 存在的理由。
    """
    events = emitter.drain()
    dropped = emitter.drain_dropped()

    if dropped:
        _log.warning(
            "事件缓冲丢弃了 %d 条（内存上限溢出）。**这个数字大于 0 就是缺陷** —— "
            "一次调用的过程会变得不完整。",
            dropped,
        )

    if not events:
        return BatchResult(dropped=dropped)

    rows = to_event_rows(events, occurred_at=now or datetime.now(timezone.utc))
    result = await EventRepo(tx).append_many(rows)
    return BatchResult(written=result.written, dropped=result.dropped + dropped)

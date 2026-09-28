"""把 gateway 的用量账本接到 repo 的用量表上。

**为什么这个映射必须住在组合根**：

- ``src/repo`` **不许** import ``src/gateway``（``NFR-R-03``：repo 在依赖链下游）；
- ``src/gateway`` **不该**知道 ``src/repo``（``NFR-G-06``：gateway 不写库，
  一旦它依赖 repo，记账失败就会拖垮模型调用 —— 而记账是**最不该失败**的东西）。

两边都不能看见对方，那么「把一个 ``UsageRecord`` 变成一个 ``usage`` 行」这件事
只能由**同时看得见两边的那个地方**做 —— 也就是这里。
这不是权宜，是那条依赖方向的直接推论。

**本模块的名字里的 "sink" 是刻意的**：它是一根**单向**的管子，
数据只从 gateway 流向 repo，没有反向的读取接口。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone

from foundation.database import Database
from foundation.logging import current_trace_id
from gateway.usage import UsageLedger, UsageRecord
from repo.types import BatchResult
from repo.usage import UsageRepo, UsageRow

__all__ = ["flush_usage", "to_usage_rows", "try_flush_usage"]

_log = logging.getLogger(__name__)


def _context_trace_id() -> str:
    """当前协程上下文的 trace_id；未设置时返回空串。

    ``foundation.logging`` 的占位符是 ``"-"``（那是「还没设置」的**显示值**），
    直接入库会让「按 trace_id 查」多出一个叫 ``-`` 的垃圾桶。
    """
    value = current_trace_id()
    return "" if value == "-" else value

#: 「本进程没有数据库」这件事只告警一次。
#:
#: 每次调用都告警会变成噪音（CLI 每次对话都会走到这里），
#: 而**完全不告警**则是最不能接受的那种静默 —— 用量在无数据库时是真的被丢掉了。
#: 一次告警是这两者之间的取舍点：看得到，且不吵。
_warned_no_database = False


def to_usage_rows(
    records: Sequence[UsageRecord], *, occurred_at: datetime
) -> list[UsageRow]:
    """把 gateway 的记录映射成 repo 的行。

    ``occurred_at`` 是**整批共用的**绝对时刻，由调用方给出。原因是
    ``UsageRecord.mono_at`` 是**单调时钟读数**（``src/gateway/usage.py:42-44``
    写明「需要绝对时间戳时由交付方在落库时补」）—— 它跨进程不可比，不能直接入库。

    代价是同一批里各条记录的时间精度被拉平到批次粒度。这在「每几秒刷一次」的
    交付节奏下是可接受的：真正需要精确时间时，应在 gateway 侧就带上绝对时刻，
    而那属于契约变更，一期不做。
    """
    # **trace_id 的归一化**：``UsageRecord.trace_id`` 来自 ``chat()`` 的**显式参数**，
    # 而事件那边的 trace_id 来自 ``foundation.logging`` 的 contextvar。
    # 调用方不传 ``trace_id=`` 时前者是空串、后者有值 ——
    # 于是用量与事件虽然同属一次调用，却**关联不起来**，而两张表恰恰是靠它串成一条线的。
    # 所以在源头补一次：显式参数优先，缺了就取上下文。
    fallback_trace = _context_trace_id()

    return [
        UsageRow(
            trace_id=record.trace_id or fallback_trace,
            # alias 可能为空（老版本 gateway 的失败记录就是空串）。
            # **不在这里拦**：拦住会让数据丢失，而告警由 UsageRepo 负责。
            alias=record.alias,
            model_key=record.model_key,
            provider=record.provider,
            model=record.model,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cached_input_tokens=record.cached_input_tokens,
            cost_amount=record.cost.amount,
            currency=record.cost.currency,
            degraded=record.degraded,
            attempt_index=record.attempt_index,
            session_id=record.session_id,
            caller=record.caller,
            occurred_at=occurred_at,
        )
        for record in records
    ]


async def flush_usage(
    database: Database,
    ledger: UsageLedger,
    *,
    now: datetime | None = None,
) -> BatchResult:
    """把账本里的记录与丢弃数**一次交付**（同一个事务）。

    Raises:
        Exception: 数据库不可用时**原样抛出**。调用方决定怎么办 ——
            见 :func:`try_flush_usage` 与 ``Runtime.flush_usage`` 的说明。
    """
    # 先取走，再进事务：drain 与 drain_dropped 之间没有 await，
    # 所以不会漏掉期间新产生的记录。
    records = ledger.drain()
    dropped = ledger.drain_dropped()

    if not records and not dropped:
        return BatchResult()

    rows = to_usage_rows(records, occurred_at=now or datetime.now(timezone.utc))
    async with database.transaction() as tx:
        return await UsageRepo(tx).record_ledger(rows, dropped=dropped)


async def try_flush_usage(
    database: Database | None,
    ledger: UsageLedger,
    *,
    now: datetime | None = None,
) -> BatchResult | None:
    """尽最大努力的交付：失败**只告警，不上抛**。

    **这条「吞掉异常」是刻意的，而且与「拒绝静默」不冲突**：
    ``NFR-R-07`` 要求记账失败不得拖垮模型调用（记账是旁路，不是依赖）。
    但吞掉的同时**必须留下告警，并说清丢了多少条** —— 静默丢弃才是被禁止的那件事。

    ``database is None``（CLI 的默认配置）时**仍然要 drain**：
    不 drain 的话账本会一直涨到 ``max_records``，然后开始**静默丢弃** ——
    正是本项目最不能接受的那种失败。取走并告警一次，是诚实的做法。

    Returns:
        ``BatchResult``；``database`` 为 ``None`` 或写入失败时返回 ``None``。
    """
    global _warned_no_database

    if database is None:
        records = ledger.drain()
        dropped = ledger.drain_dropped()
        if (records or dropped) and not _warned_no_database:
            _warned_no_database = True
            _log.warning(
                "本进程没有配置数据库，用量记录**不会被持久化**。"
                "已取走 %d 条（丢弃 %d 条）并丢弃 —— 这是 CLI 的默认行为。"
                "需要留存请注入 database（见 apps/cli/storage/）。"
                "本告警每进程只出现一次，后续同样会被丢弃。",
                len(records),
                dropped,
            )
        return None

    try:
        return await flush_usage(database, ledger, now=now)
    except Exception:  # noqa: BLE001 —— 记账是旁路，任何失败都不该上抛
        # 注意：记录已经从 ledger 里 drain 出来了，所以这次失败**真的丢了数据**。
        # 告警必须说清这一点，否则「账少了」的原因会变成一个查不到的空缺。
        _log.exception(
            "用量落库失败，本次已 drain 的记录将丢失。"
            "记账是旁路（NFR-R-07），不影响已完成/进行中的模型调用，"
            "但账单会少这几笔。请检查数据库可用性。"
        )
        return None

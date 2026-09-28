"""用量落库（``repo.usage``）与交付（``composition.usage_sink``）。

用内存 SQLite 跑（``NFR-R-04``：不得让单测依赖本机数据库）。
真实 Postgres 上的端到端另见 ``tests/integration/storage/``。

**这一组测的是一根曾经断掉的线**：在 ``repo.usage`` 出现之前，
``UsageLedger.drain()`` 全仓库零个调用方，``dropped`` 零处读取 ——
用量记在内存里，然后随进程一起消失，而且**不会有任何错误**。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from composition.usage_sink import flush_usage, to_usage_rows, try_flush_usage
from foundation.database import Database
from foundation.db import Base, create_engine
from gateway.types import Cost
from gateway.usage import UsageLedger, UsageRecord
from repo.types import BatchResult
from repo.usage import UsageRepo, UsageRow


@pytest.fixture
async def db():
    """内存 SQLite + 建表。

    注意这里用 ``create_all`` 而不是迁移：单测需要的是**结构**，
    而迁移的正确性由 ``tests/integration`` 在真实 Postgres 上验。
    """
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.aclose()


def _record(**over) -> UsageRecord:
    base = {
        "trace_id": "t-1",
        "alias": "runtime.default",
        "model_key": "m1",
        "provider": "deepseek",
        "model": "deepseek-chat",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost": Cost(currency="CNY", amount=Decimal("0.000089")),
    }
    base.update(over)
    return UsageRecord(**base)


# --------------------------------------------------------------------------- #
# CR-2 未知不落 0
# --------------------------------------------------------------------------- #


async def test_unknown_usage_is_stored_as_null_not_zero(db: Database):
    """**``NULL`` 与 ``0`` 是两件事。**

    上游没返回用量时落 ``NULL``（不知道），不是 ``0``（确实用了 0 个）。
    混同会让成本报表静默失真 —— 而这条不变量**无法用 DDL 强制**，
    所以它靠代码纪律守，并由本用例钉住。
    """
    async with db.transaction() as tx:
        await UsageRepo(tx).record_ledger(
            [
                UsageRow(
                    trace_id="t-null", alias="runtime.default", model_key="m1",
                    input_tokens=None, output_tokens=None,
                    occurred_at=datetime.now(timezone.utc),
                )
            ]
        )

    async with db.transaction() as tx:
        nulls = await tx.scalar(text("SELECT count(*) FROM usage WHERE input_tokens IS NULL"))
        zeros = await tx.scalar(text("SELECT count(*) FROM usage WHERE input_tokens = 0"))
    assert nulls == 1
    assert zeros == 0, "未知用量落成了 0 —— 报表会静默失真"


async def test_unknown_cost_is_stored_as_null(db: Database):
    """价格没配时成本留 ``NULL``，不是 0 —— 0 是「免费」。"""
    async with db.transaction() as tx:
        await UsageRepo(tx).record_ledger(
            [
                UsageRow(
                    trace_id="t-nocost", alias="runtime.default", model_key="m1",
                    cost_amount=None, occurred_at=datetime.now(timezone.utc),
                )
            ]
        )
    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage WHERE cost_amount IS NULL")) == 1


# --------------------------------------------------------------------------- #
# CR-3 dropped 有出口
# --------------------------------------------------------------------------- #


async def test_dropped_is_readable_from_the_database(db: Database):
    """丢弃数必须能被查到。

    gateway 的 ``UsageLedger.dropped`` 是前车之鉴：它记了丢弃数，
    但**全仓库没有一处读它** —— 于是「账单少了几笔」这件事没有任何出口，
    报表上看起来一切正常。日志会被轮转掉，所以还需要能查。
    """
    async with db.transaction() as tx:
        result = await UsageRepo(tx).record_ledger([], dropped=7)

    assert result.written == 0
    async with db.transaction() as tx:
        stored = await tx.scalar(text("SELECT sum(dropped_count) FROM usage_drops"))
    assert stored == 7


async def test_zero_dropped_writes_no_row(db: Database):
    """``dropped == 0`` 时**不写行** —— 否则每次正常交付都留一条无意义的记录。"""
    async with db.transaction() as tx:
        await UsageRepo(tx).record_ledger([], dropped=0)
    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage_drops")) == 0


async def test_dropped_is_logged_as_a_defect(db: Database, caplog):
    """丢数据必须**出声**，而且是 WARNING 级 —— 它意味着账单会少几笔。"""
    with caplog.at_level(logging.WARNING, logger="repo.usage"):
        async with db.transaction() as tx:
            await UsageRepo(tx).record_ledger([], dropped=3)

    assert any("缺陷" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 原子性：记录与丢弃数在同一个事务
# --------------------------------------------------------------------------- #


async def test_records_and_dropped_land_in_one_transaction(db: Database):
    """记录与丢弃数必须**同事务**。

    否则会出现「记录写了、丢弃数没写」的窗口 ——
    而那个窗口里丢掉的数据永远不会被任何人发现。
    """
    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await UsageRepo(tx).record_ledger(
                [
                    UsageRow(
                        trace_id="t-x", alias="a", model_key="m1",
                        occurred_at=datetime.now(timezone.utc),
                    )
                ],
                dropped=5,
            )
            raise RuntimeError("业务失败")

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM usage_drops")) == 0


# --------------------------------------------------------------------------- #
# alias 缺失：存下来 + 告警（保住数据优先）
# --------------------------------------------------------------------------- #


async def test_blank_alias_warns_but_still_stores(db: Database, caplog):
    """``alias`` 为空时**照存不误，但告警**。

    取舍：丢掉它们会让账单真的少一块（数据不可恢复），
    存下来只是聚合时漏掉 —— 两害相权，保住数据。
    """
    with caplog.at_level(logging.WARNING, logger="repo.usage"):
        async with db.transaction() as tx:
            await UsageRepo(tx).record_ledger(
                [
                    UsageRow(
                        trace_id="t-blank", alias="", model_key="m1",
                        occurred_at=datetime.now(timezone.utc),
                    )
                ]
            )

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage")) == 1, "数据必须保住"
    assert any("逻辑名" in r.message for r in caplog.records), "必须告警"


# --------------------------------------------------------------------------- #
# 交付链路（usage_sink）
# --------------------------------------------------------------------------- #


def test_to_usage_rows_maps_every_aggregation_dimension():
    """四个聚合维度一个都不能漏 —— 漏一个就没有聚合路径了（``FR-G-08``）。"""
    rows = to_usage_rows([_record(session_id="s-1", caller="agent-x")], occurred_at=datetime.now(timezone.utc))
    row = rows[0]
    assert (row.alias, row.model_key, row.session_id, row.caller) == (
        "runtime.default", "m1", "s-1", "agent-x",
    )


async def test_flush_usage_delivers_and_consumes(db: Database):
    """``flush_usage`` 要一次把「记录 + 丢弃数」都交付，并且**都取走**。"""
    ledger = UsageLedger(max_records=1)
    for i in range(3):
        ledger.record(_record(trace_id=f"t-{i}"))

    result = await flush_usage(db, ledger)
    assert result is not None and result.written == 1  # 上限 1，最后一条留着

    assert ledger.records == (), "记录必须被取走"
    assert ledger.dropped == 0, "丢弃数也必须被取走并清零，否则会被重复上报"

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage")) == 1
        assert await tx.scalar(text("SELECT sum(dropped_count) FROM usage_drops")) == 2


async def test_flush_usage_is_a_noop_when_empty(db: Database):
    """没东西可交时不进事务、不写行。"""
    assert await flush_usage(db, UsageLedger()) == BatchResult()
    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage_drops")) == 0


async def test_try_flush_swallows_failure_but_screams(db: Database, caplog):
    """记账失败**不得上抛**（``NFR-R-07``：记账是旁路），但必须告警。

    **吞掉异常是刻意的**：一次数据库抖动不该让模型调用失败。
    但吞掉的同时必须说清「这次真的丢了数据」——
    记录已经从账本里 drain 出来了，失败就是真的丢。
    """
    ledger = UsageLedger()
    ledger.record(_record(trace_id="t-lost"))
    await db.aclose()  # 把库关掉，制造写入失败

    with caplog.at_level(logging.ERROR, logger="composition.usage_sink"):
        result = await try_flush_usage(db, ledger)

    assert result is None, "失败时返回 None，而不是抛异常"
    assert any("丢失" in r.message for r in caplog.records), "必须说清丢了数据"


async def test_try_flush_without_database_still_drains(caplog):
    """没有数据库（CLI 默认）时**仍然要取走**，并且**告警一次**。

    不取走的话账本会一直涨到 ``max_records`` 然后开始静默丢弃 ——
    正是本项目最不能接受的那种失败。告警一次是为了「看得到，但不吵」：
    每次对话都告警会变噪音，完全不告警则是最糟的静默。
    """
    import composition.usage_sink as sink

    sink._warned_no_database = False  # 每个用例独立，避免被其它用例提前置位
    ledger = UsageLedger()
    ledger.record(_record())

    with caplog.at_level(logging.WARNING, logger="composition.usage_sink"):
        assert await try_flush_usage(None, ledger) is None

    assert ledger.records == (), "即使不落库也要取走，避免无界增长"
    assert any("不会被持久化" in r.message for r in caplog.records), "必须告警一次"

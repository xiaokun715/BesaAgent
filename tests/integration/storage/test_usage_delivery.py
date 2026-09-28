"""用量交付链路的端到端验证（真实 Postgres）。

**走的是完整的那条链**：``UsageLedger`` → ``composition.usage_sink`` → ``repo.usage``
→ 迁移建出来的表。单测用 SQLite 跑，这里用真实后端 ——
两者的差别是实打实的（``NFR-R-06`` 的批量写在 Postgres 上是 executemany、
在 SQLite 上是逐行；``DEFAULT now()`` 只在 Postgres 上存在）。

**前提**：``besa_agent`` 库已建、迁移已跑（``alembic upgrade head``）。
不满足时整组跳过。

为了不污染共享库，写入的记录都带一个独特的 ``trace_id`` 前缀，用例结束前删干净。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from apps.server.storage.postgres import open_database
from composition.usage_sink import flush_usage
from foundation.settings import load_config
from gateway.types import Cost
from gateway.usage import UsageLedger, UsageRecord

#: 测试用的库与角色。与 ``test_postgres_guard.py`` 保持一致。
TEST_DB = "postgresql://besa:besa@127.0.0.1:5432/besa_agent"


def _reachable() -> bool:
    """库可达**且迁移已跑**才算就绪 —— 只有表存在才说明迁移跑过。"""
    try:
        import psycopg

        with psycopg.connect(TEST_DB, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('usage') IS NOT NULL")
                return bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason="besa_agent 不可达或迁移未跑，跳过真实库交付测试"
)


@pytest.fixture
async def db():
    cfg = load_config("dev")
    database = await open_database(cfg.section("postgres"), require_vector=False)
    try:
        yield database
    finally:
        await database.aclose()


@pytest.fixture
def tag() -> str:
    """本用例的唯一前缀，用完好清理。"""
    return f"itest-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
async def _cleanup(db, tag):
    yield
    async with db.transaction() as tx:
        await tx.execute(text("DELETE FROM usage WHERE trace_id LIKE :p"), {"p": f"{tag}%"})


def _record(tag: str, **over) -> UsageRecord:
    base = {
        "trace_id": f"{tag}-1",
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


async def test_full_delivery_chain_lands_in_postgres(db, tag):
    """一次交付把记录送进 ``usage`` 表，字段一个不少。"""
    ledger = UsageLedger()
    ledger.record(_record(tag, session_id="s-1", caller="agent-x"))

    result = await flush_usage(db, ledger, now=datetime.now(timezone.utc))
    assert result is not None and result.written == 1

    async with db.transaction() as tx:
        row = (
            await tx.execute(
                text(
                    "SELECT alias, model_key, input_tokens, cost_amount, currency, "
                    "session_id, caller FROM usage WHERE trace_id = :t"
                ),
                {"t": f"{tag}-1"},
            )
        ).one()

    assert row.alias == "runtime.default"
    assert row.input_tokens == 100
    assert row.cost_amount == Decimal("0.000089")
    assert (row.session_id, row.caller) == ("s-1", "agent-x")


async def test_unknown_usage_survives_the_round_trip_as_null(db, tag):
    """未知用量经过真实 Postgres 一个来回之后**仍然是 NULL**，没有变成 0。"""
    ledger = UsageLedger()
    ledger.record(_record(tag, input_tokens=None, output_tokens=None, cost=Cost(currency="CNY")))

    await flush_usage(db, ledger, now=datetime.now(timezone.utc))

    async with db.transaction() as tx:
        input_tokens = await tx.scalar(
            text("SELECT input_tokens FROM usage WHERE trace_id = :t"), {"t": f"{tag}-1"}
        )
        cost = await tx.scalar(
            text("SELECT cost_amount FROM usage WHERE trace_id = :t"), {"t": f"{tag}-1"}
        )
    assert input_tokens is None, "未知用量落成了 0 —— 报表会静默失真"
    assert cost is None, "未知成本落成了 0 —— 0 是「免费」"


async def test_dropped_has_an_exit_in_the_database(db, tag):
    """丢弃数在真实库里可查 —— 这就是 gateway 那个「没有出口」的计数器的终点。"""
    ledger = UsageLedger(max_records=1)
    for i in range(4):
        ledger.record(_record(tag, trace_id=f"{tag}-{i}"))

    await flush_usage(db, ledger, now=datetime.now(timezone.utc))

    async with db.transaction() as tx:
        dropped = await tx.scalar(text("SELECT sum(dropped_count) FROM usage_drops"))
    assert dropped and dropped >= 3


async def test_migration_created_all_three_columns_as_nullable(db, tag):
    """结构断言：三个 token 列**必须**可空。

    它们若被建成 ``NOT NULL``，那么「上游没返回用量」就无处可写 ——
    代码只能补一个 0，而那就正好是 ``NFR-R-08`` 禁止的事。
    """
    async with db.transaction() as tx:
        rows = await tx.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'usage' "
                "AND column_name IN ('input_tokens','output_tokens','cached_input_tokens')"
            )
        )
        columns = {r.column_name: r.is_nullable for r in rows}

    assert columns == {
        "input_tokens": "YES",
        "output_tokens": "YES",
        "cached_input_tokens": "YES",
    }


async def test_delivery_is_idempotent_across_repeated_flushes(db, tag):
    """连续交付两次：第二次没东西可交，**不重复写**。"""
    ledger = UsageLedger()
    ledger.record(_record(tag))

    first = await flush_usage(db, ledger, now=datetime.now(timezone.utc))
    second = await flush_usage(db, ledger, now=datetime.now(timezone.utc))

    assert first is not None and first.written == 1
    assert second is None or second.written == 0

    async with db.transaction() as tx:
        n = await tx.scalar(
            text("SELECT count(*) FROM usage WHERE trace_id = :t"), {"t": f"{tag}-1"}
        )
    assert n == 1

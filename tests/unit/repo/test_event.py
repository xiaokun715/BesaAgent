"""事件落库（``repo.event``）与事件缓冲（``composition.event_sink``）。

**为什么这组值得写**：事件表的价值全在「**能还原一次调用的过程**」。
而它失效的方式同样安静 —— 少一条事件、顺序乱了、重复了两条，
都不会让任何功能出错，只是排障时看到的图景是错的。
所以每条断言都冲着「还原出的过程是否可信」去。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from composition.event_sink import BufferingEmitter, flush_events, to_event_rows
from foundation.database import Database
from foundation.db import Base, create_engine
from foundation.logging import reset_trace_id, set_trace_id
from repo.event import EventRepo, EventRow
from repo.types import TimeRange

# **必须模块级 import**：`db` 夹具里的 ``create_all`` 只建「此刻已注册到
# ``Base.metadata`` 的表」。在用例函数里才 import 的话，表还没注册就已经建完了，
# 表现为 ``no such table: usage`` —— 而报错指向的却是那次插入。
from repo.usage import UsageRepo, UsageRow  # noqa: E402


@pytest.fixture
def set_trace():
    """设置 trace_id 并在用例结束后复位。

    ``reset_trace_id`` **需要 token**（不是无参复位）—— 因为它恢复的是
    「进入本上下文之前」的值，而不是强制清空。用 fixture 拿 token 是唯一正确的用法；
    直接在用例里不传参调用会报 ``TypeError``。
    """
    token = None

    def _set(value: str) -> None:
        nonlocal token
        token = set_trace_id(value)

    yield _set
    if token is not None:
        reset_trace_id(token)


@pytest.fixture
async def db():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.aclose()


def _row(name: str = "gateway.call.started", **over) -> EventRow:
    base = {
        "event_id": f"evt-{name}-{over.get('attempt_index', 0)}",
        "name": name,
        "trace_id": "t-1",
        "alias": "runtime.default",
        "model_key": "m1",
        "payload": {"alias": "runtime.default"},
        "occurred_at": datetime.now(timezone.utc),
    }
    base.update(over)
    return EventRow(**base)


# --------------------------------------------------------------------------- #
# append-only 是结构性的，不是靠约定
# --------------------------------------------------------------------------- #


def test_event_repo_exposes_no_update_or_delete():
    """**不得**提供修改与删除。

    这条是结构性的：不是靠「你不要改」，而是**没有那个方法**。
    给事件加上 update/delete 就等于给了「事后修改历史」的能力 ——
    而排查故障时最不能接受的，就是「这条记录可能被改过」。
    """
    for forbidden in ("update", "delete", "remove", "purge"):
        assert not hasattr(EventRepo, forbidden), (
            f"EventRepo 不应提供 {forbidden}()：事件是 append-only，改历史等于毁证据"
        )


# --------------------------------------------------------------------------- #
# 写入与幂等
# --------------------------------------------------------------------------- #


async def test_append_many_writes_rows(db: Database):
    async with db.transaction() as tx:
        result = await EventRepo(tx).append_many([_row(), _row(attempt_index=1)])
    assert result.written == 2
    assert result.dropped == 0

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM event")) == 2


async def test_duplicate_event_id_is_ignored_not_an_error(db: Database, caplog):
    """**重复交付是幂等的，而且被忽略的条数要报告出来。**

    交付方可能在「写成功但确认丢失」之后重试 —— 那在分布式里是正常现象，
    不该让整批失败，也不该产生重复行。

    但**也不能静默**：同一批被交付两次本身是个值得注意的信号，
    所以被忽略的条数要计入 ``BatchResult.dropped`` 并告警。
    """
    same = _row()
    async with db.transaction() as tx:
        await EventRepo(tx).append_many([same])

    with caplog.at_level(logging.WARNING, logger="repo.event"):
        async with db.transaction() as tx:
            result = await EventRepo(tx).append_many([same])

    assert result.written == 0
    assert result.dropped == 1, "被忽略的条数必须报告，不能静默吞掉"
    assert any("重复" in r.message for r in caplog.records)

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM event")) == 1, "不得产生重复行"


# --------------------------------------------------------------------------- #
# 读：还原一次调用
# --------------------------------------------------------------------------- #


async def test_by_trace_returns_events_in_order(db: Database):
    """按 trace 取回的必须是**有序**的一条线。

    排序用 ``(occurred_at, id)`` 而不是只靠时间：同一批交付的事件时间戳是
    被拉平的（见 ``event_sink`` 的说明），只按时间排的话
    「先重试还是先降级」会看不出来 —— 而那正是排障要看的东西。
    """
    now = datetime.now(timezone.utc)
    events = [
        _row("gateway.call.started", event_id="e1", attempt_index=0, occurred_at=now),
        _row("gateway.call.failed", event_id="e2", attempt_index=0, occurred_at=now),
        _row("gateway.call.retried", event_id="e3", attempt_index=1, occurred_at=now),
        _row("gateway.call.succeeded", event_id="e4", attempt_index=1, occurred_at=now),
    ]
    async with db.transaction() as tx:
        await EventRepo(tx).append_many(events)
    async with db.transaction() as tx:
        got = await EventRepo(tx).by_trace("t-1")

    assert [e.event_id for e in got] == ["e1", "e2", "e3", "e4"], (
        "时间戳被拉平时必须靠自增 id 兜底，否则调用过程会乱序"
    )


async def test_by_session_and_time_window(db: Database):
    now = datetime.now(timezone.utc)
    async with db.transaction() as tx:
        await EventRepo(tx).append_many([
            _row(event_id="s1", session_id="s-1", occurred_at=now - timedelta(hours=2)),
            _row(event_id="s2", session_id="s-1", occurred_at=now),
            _row(event_id="s3", session_id="s-2", occurred_at=now),
        ])

    async with db.transaction() as tx:
        rows = await EventRepo(tx).by_session("s-1")
    assert [r.event_id for r in rows] == ["s2", "s1"], "最近的在前"

    async with db.transaction() as tx:
        rows = await EventRepo(tx).by_session(
            "s-1", window=TimeRange(start=now - timedelta(hours=1))
        )
    assert [r.event_id for r in rows] == ["s2"], "时间范围是半开区间"


async def test_count_by_name(db: Database):
    async with db.transaction() as tx:
        await EventRepo(tx).append_many([
            _row("gateway.call.failed", event_id="c1"),
            _row("gateway.call.failed", event_id="c2"),
            _row("gateway.call.succeeded", event_id="c3"),
        ])
    async with db.transaction() as tx:
        counts = await EventRepo(tx).count_by_name()
    assert counts == {"gateway.call.failed": 2, "gateway.call.succeeded": 1}


# --------------------------------------------------------------------------- #
# 缓冲器
# --------------------------------------------------------------------------- #


def test_emitter_buffers_and_drains():
    sink = BufferingEmitter()
    sink.emit("a", {"x": 1})
    sink.emit("b", {"x": 2})
    assert sink.pending == 2

    drained = sink.drain()
    assert [name for name, _ in drained] == ["a", "b"]
    assert sink.pending == 0, "取走之后必须清空"


def test_emitter_drops_oldest_and_reports(db, caplog):
    """溢出时丢**最旧**的并计数 —— 丢旧账比崩进程好，但必须可见。

    与 ``UsageLedger`` 同样的权衡：无界缓冲会把「落库变慢」升级成「进程 OOM」。
    """
    sink = BufferingEmitter(max_records=2)
    for i in range(5):
        sink.emit("e", {"i": i})

    assert sink.pending == 2
    assert sink.dropped == 3
    assert sink.drain_dropped() == 3
    assert sink.drain_dropped() == 0, "取走之后必须清零，否则同一笔会被重复上报"


def test_emitter_never_raises_on_emit():
    """``emit`` 必须永不抛异常 —— gateway 的 ``_emit`` 只兜一层，
    而且它兜掉之后失败原因只打 debug 日志。在这里保持「不做 IO、不校验」最稳。"""
    sink = BufferingEmitter()
    sink.emit("x", {})          # 空载荷
    sink.emit("", {})           # 空名字
    assert sink.pending == 2


# --------------------------------------------------------------------------- #
# 映射：trace_id 从上下文来，session/caller 暂无
# --------------------------------------------------------------------------- #


def test_trace_id_comes_from_the_logging_context(set_trace):
    """**不需要改 gateway**：``foundation.logging`` 已经用 contextvar 承载了 trace_id。

    这也是它必须是 contextvar 而不是全局变量的原因 ——
    多 agent 并发时，全局变量会让 A 调用的事件挂上 B 的 trace_id。
    """
    set_trace("trace-from-context")
    rows = to_event_rows([("gateway.call.started", {})], occurred_at=datetime.now(timezone.utc))
    assert rows[0].trace_id == "trace-from-context"


def test_placeholder_trace_id_is_normalized_to_empty(set_trace):
    """日志模块的占位符 ``"-"`` 不是 trace_id，不能存进去 ——
    否则「按 trace_id 查」会多出一个叫 ``-`` 的垃圾桶。"""
    set_trace("-")
    rows = to_event_rows([("e", {})], occurred_at=datetime.now(timezone.utc))
    assert rows[0].trace_id == ""


def test_payload_trace_id_wins_over_the_context(set_trace):
    """载荷里自己带了就以它为准 —— 它比上下文更具体。"""
    set_trace("outer")
    rows = to_event_rows(
        [("e", {"trace_id": "inner"})], occurred_at=datetime.now(timezone.utc)
    )
    assert rows[0].trace_id == "inner"


def test_session_and_caller_are_null_when_gateway_does_not_provide_them():
    """**记录一条已知缺口**，而不是假装它不存在。

    gateway 的 7 个 emit 点都不带 ``session_id`` / ``caller``（本次架构分析记录在案）。
    这两个字段在本表里可空，所以事件照样能存 —— 但「按会话查事件」暂时查不出东西。

    这条用例的作用是：**当那天有人补上 gateway 时，它会红**，
    提醒把这行断言改成「必须非空」。
    """
    rows = to_event_rows(
        [("gateway.call.started", {"alias": "runtime.default"})],
        occurred_at=datetime.now(timezone.utc),
    )
    assert rows[0].session_id is None
    assert rows[0].caller is None


# --------------------------------------------------------------------------- #
# 交付：与用量同一个事务（foundation/database.py 的核心目标）
# --------------------------------------------------------------------------- #


async def test_flush_events_writes_into_the_given_transaction(db: Database):
    """收 ``tx`` 而不是 ``Database`` —— 这是它能与用量同事务的前提。"""
    sink = BufferingEmitter()
    sink.emit("gateway.call.started", {"alias": "runtime.default"})

    async with db.transaction() as tx:
        result = await flush_events(tx, sink)

    assert result.written == 1
    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM event")) == 1


async def test_events_and_usage_roll_back_together(db: Database):
    """**事件与用量必须一起成功或一起回滚。**

    这正是 ``foundation/database.py`` 存在的理由：
    「事件说降级了、用量里没有对应记录」这种矛盾，事后谁都说不清是哪边丢了。
    """
    now = datetime.now(timezone.utc)
    sink = BufferingEmitter()
    sink.emit("gateway.call.started", {"trace_id": "t-rollback"})

    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await flush_events(tx, sink)
            await UsageRepo(tx).record_ledger([
                UsageRow(trace_id="t-rollback", alias="a", model_key="m1", occurred_at=now)
            ])
            raise RuntimeError("业务失败")

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM event")) == 0
        assert await tx.scalar(text("SELECT count(*) FROM usage")) == 0

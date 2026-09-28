"""事务边界（``foundation/database.py``）的测试。

**为什么这些用例值得单独写一组**：本模块存在的全部理由是
「一次业务操作 = 一个事务」，而它的失效模式**全都不报错** ——
多开一个事务、少提交一次、嵌套后各提交各的，全都会安静地过去。
所以这里的每条断言都是冲着某个**静默失败**去的。

用 SQLite 内存库跑，不需要任何外部服务（``NFR-R-04``）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from foundation.database import Database
from foundation.db import create_engine

MEMORY = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def db() -> Database:
    return Database(create_engine(MEMORY))


async def _count(tx) -> int:
    return await tx.scalar(text("SELECT count(*) FROM t"))


@pytest.fixture
async def seeded(db: Database):
    """建一张表并写入 2 行，作为后续用例的基线。"""
    async with db.transaction() as tx:
        await tx.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        await tx.execute(text("INSERT INTO t (v) VALUES ('a'), ('b')"))
    yield db
    await db.aclose()


# --------------------------------------------------------------------------- #
# 一、边界的基本语义
# --------------------------------------------------------------------------- #


async def test_commit_on_clean_exit(db: Database):
    """正常出块 = 提交。"""
    async with db.transaction() as tx:
        await tx.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    async with db.transaction() as tx:
        await tx.execute(text("INSERT INTO t (v) VALUES ('x')"))
        assert await _count(tx) == 1

    async with db.transaction() as tx:
        assert await _count(tx) == 1, "上一个事务提交的写入应当可见"
    await db.aclose()


async def test_rollback_on_exception(seeded: Database):
    """抛异常 = 回滚，**且不是只回滚当前语句**。"""
    with pytest.raises(ValueError):
        async with seeded.transaction() as tx:
            await tx.execute(text("INSERT INTO t (v) VALUES ('c')"))
            await tx.execute(text("INSERT INTO t (v) VALUES ('d')"))
            raise ValueError("业务失败")

    async with seeded.transaction() as tx:
        assert await _count(tx) == 2, "异常前写入的每一行都必须一起回滚"


async def test_memory_sqlite_shares_one_database_across_transactions(seeded: Database):
    """内存 SQLite 必须是**同一个库**。

    默认每个连接各自是一个空库，于是「建表」与「查表」会落在两个不同的库里 ——
    表现是「表不存在」，而建表那句明明刚成功过。
    ``foundation/db.py`` 用 ``StaticPool`` 挡住了它，这条用例是那个决定的守卫。
    """
    async with seeded.transaction() as tx:
        assert await _count(tx) == 2


# --------------------------------------------------------------------------- #
# 二、嵌套 —— 本模块最要防的那个静默失败
# --------------------------------------------------------------------------- #


async def test_nested_transaction_is_rejected(seeded: Database):
    """**嵌套必须报错，不能各开各的。**

    嵌套调用会各建一个会话 = 两个独立事务：外层提交、内层回滚，
    数据处于谁也没预期过的状态，而**两边都不会报错**。
    这正是本模块要消灭的事故形态，所以它必须是一条明确的错误。
    """
    with pytest.raises(RuntimeError, match="事务不能嵌套"):
        async with seeded.transaction():
            async with seeded.transaction():
                pass  # pragma: no cover


async def test_nesting_error_message_says_what_to_do(seeded: Database):
    """错误信息要能指导改法 —— 只说「不能嵌套」会让人卡住。"""
    with pytest.raises(RuntimeError) as excinfo:
        async with seeded.transaction():
            async with seeded.transaction():
                pass  # pragma: no cover

    message = str(excinfo.value)
    assert "共用同一个" in message, "要告诉人怎么改：共用同一个 async with 块"
    assert "事务块**之外**" in message or "之外" in message, "并行任务要在事务外创建"


async def test_concurrent_transactions_are_not_mistaken_for_nesting(seeded: Database):
    """**并发的独立事务不能被误判成嵌套。**

    这是用 ContextVar 而不是实例属性的理由：数据库对象与并发协程是一对多的，
    一个 ``self._busy = True`` 会被另一个协程看见。
    """
    async def worker(i: int) -> int:
        async with seeded.transaction() as tx:
            await tx.execute(text("SELECT 1"))
            return i

    results = await asyncio.gather(*(worker(i) for i in range(8)))
    assert results == list(range(8))


async def test_transaction_flag_is_reset_after_failure(seeded: Database):
    """异常退出后标志必须复位 —— 否则一次失败会**永久**毒化这个 Database。"""
    with pytest.raises(ValueError):
        async with seeded.transaction():
            raise ValueError("boom")

    # 还能正常开新事务（若 ContextVar 没复位，这里会报「不能嵌套」）
    async with seeded.transaction() as tx:
        assert await _count(tx) == 2


# --------------------------------------------------------------------------- #
# 三、结构约束：拿不到 commit 的权利
# --------------------------------------------------------------------------- #


def test_transaction_exposes_no_commit_or_rollback():
    """``Transaction`` **不得**暴露 commit / rollback。

    这是「把选择的自由收走」在**类接口**上的样子：不是靠约定「你不要 commit」，
    而是没有那个方法。这条断言是结构性的 —— 有人加了那两个方法，它会红。
    """
    from foundation.database import Transaction

    for forbidden in ("commit", "rollback", "begin", "close"):
        assert not hasattr(Transaction, forbidden), (
            f"Transaction 不应暴露 {forbidden}()：事务边界只能由 Database.transaction 开关"
        )


def test_database_exposes_engine_for_migrations_only():
    """``engine`` 必须可读（迁移与健康检查要用），但它的 docstring 要写明只给装配层。"""
    from foundation.database import Database

    doc = Database.engine.__doc__ or ""
    assert "只给装配层" in doc or "不得使用" in doc, (
        "engine 是个危险的出口：仓储一旦绕过 transaction() 直接拿它，事务边界就漏了"
    )


# --------------------------------------------------------------------------- #
# 四、DSN 校验
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("dsn", "hint"),
    [
        ("postgresql://u:p@h/db", "+asyncpg"),
        ("postgres://u:p@h/db", "+asyncpg"),
        ("sqlite:///tmp/x.db", "+aiosqlite"),
    ],
)
def test_sync_driver_is_rejected_with_a_hint(dsn: str, hint: str):
    """同步驱动必须在**构造期**就报错，并告诉人该补什么。

    放到运行时才炸的话，报错点会在第一次 await 上，离这里很远。
    """
    with pytest.raises(ValueError) as excinfo:
        create_engine(dsn)
    assert hint in str(excinfo.value)


def test_empty_dsn_is_rejected():
    with pytest.raises(ValueError, match="为空或格式不对"):
        create_engine("")


def test_async_drivers_are_accepted():
    for dsn in ("postgresql+asyncpg://u:p@127.0.0.1:5432/x", MEMORY):
        engine = create_engine(dsn)
        assert engine is not None

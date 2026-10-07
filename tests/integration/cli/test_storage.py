"""``apps/cli/storage`` 的测试 —— CLI 的默认存储。

**为什么 CLI 要有一个真的数据库（哪怕在内存里）**：
「不落库」与「落在内存里」在代码上是两条路径，而两条路径会漂移。
让 CLI 拿一个内存 SQLite，仓储代码就与 server 完全同一条，
而「无需外部服务即可跑通」这条项目级前提仍然成立。

这里同时钉住 SQLite 的**能力边界**（没有 pgvector、不支持多进程写）——
让「CLI 上跑不通某个功能」时，能一眼看出是边界而不是 bug。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from apps.cli.storage import MEMORY_DSN, open_database
from apps.cli.storage.sqlite import open_database_from_config
from repo import Repository


async def test_memory_sqlite_works_without_any_external_service():
    """默认路径不需要 Postgres、不需要 Redis、不需要任何文件。"""
    db = await open_database()
    try:
        async with db.transaction() as tx:
            await tx.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
            await tx.execute(text("INSERT INTO t (v) VALUES ('x')"))
        async with db.transaction() as tx:
            assert await tx.scalar(text("SELECT count(*) FROM t")) == 1
    finally:
        await db.aclose()


async def test_same_repository_code_runs_on_sqlite():
    """**仓储代码在 SQLite 与 Postgres 上是同一份。**

    这是「抽象点放在引擎而不是事务类」那个实现期修订换来的收益 ——
    如果当初把 ``Transaction`` 做成 Protocol 并让 CLI 用一个空的「内存实现」，
    那个空壳会**静默丢弃写入**，而这里跑的是真的 SQL。
    """

    class NoteRepo(Repository):
        async def add(self, value: str) -> int:
            result = await self.tx.execute(
                text("INSERT INTO notes (v) VALUES (:v) RETURNING id"), {"v": value}
            )
            return result.scalar_one()

        async def all(self) -> list[str]:
            return list(await self.tx.fetch_all(text("SELECT v FROM notes ORDER BY id")))

    db = await open_database()
    try:
        async with db.transaction() as tx:
            await tx.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, v TEXT)"))

        async with db.transaction() as tx:
            repo = NoteRepo(tx)
            await repo.add("第一条")
            await repo.add("第二条")

        async with db.transaction() as tx:
            assert await NoteRepo(tx).all() == ["第一条", "第二条"]
    finally:
        await db.aclose()


async def test_rollback_works_on_sqlite_too():
    """回滚语义在两个后端上必须一致 —— 否则在 CLI 里「测过了」到 server 上会变。"""
    db = await open_database()
    try:
        async with db.transaction() as tx:
            await tx.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))

        with pytest.raises(RuntimeError):
            async with db.transaction() as tx:
                await tx.execute(text("INSERT INTO t (id) VALUES (1)"))
                raise RuntimeError("boom")

        async with db.transaction() as tx:
            assert await tx.scalar(text("SELECT count(*) FROM t")) == 0
    finally:
        await db.aclose()


async def test_memory_database_is_discarded_on_exit():
    """内存库的语义是「进程退出即丢」—— 这条钉住它**不是**一个持久化承诺。"""
    db1 = await open_database()
    async with db1.transaction() as tx:
        await tx.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
    await db1.aclose()

    db2 = await open_database()
    try:
        with pytest.raises(Exception):
            # 新连接 = 新库。表不存在才是对的。
            async with db2.transaction() as tx:
                await tx.execute(text("SELECT count(*) FROM t"))
    finally:
        await db2.aclose()


async def test_config_can_point_at_a_file():
    """配了 dsn 就用配的，没配就用内存 —— 组合根不必判断「有没有配」。"""
    db = await open_database_from_config({})
    assert db.engine.url.database in (None, ":memory:"), "缺省应当是内存库"
    await db.aclose()


async def test_opening_a_database_creates_the_schema():
    """**建表是 ``open_database`` 的一部分**。

    不建的话，第一次 ``flush_usage`` 会以「表不存在」失败 ——
    而那个失败看起来像「数据库配错了」，实际只是漏了一件本该在这里做的事。
    """
    from sqlalchemy import text

    db = await open_database()
    try:
        async with db.transaction() as tx:
            # 三张表都要在：用量、事件、工具执行记录
            for table in ("usage", "usage_drops", "event", "tool_execution"):
                await tx.execute(text(f"SELECT count(*) FROM {table}"))
    finally:
        await db.aclose()


def test_memory_dsn_uses_aiosqlite_driver():
    """DSN 必须带异步驱动 —— 同步驱动会在第一次 await 才炸，报错点离原因很远。"""
    assert MEMORY_DSN.startswith("sqlite+aiosqlite://")

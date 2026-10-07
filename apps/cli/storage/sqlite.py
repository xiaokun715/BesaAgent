"""SQLite 后端：CLI 的默认存储，也可用作开发期的临时库。

**与 Postgres 用的是同一套代码**：``foundation.database.Database`` 只认引擎，
不认识方言。所以 CLI 与 server 跑的是**同一份仓储实现** ——
这正是「抽象点放在引擎而不是事务类」那个修订（见 ``foundation/database.py``
模块 docstring 的「实现期修订」）换来的收益。

**能力边界（必须知道，否则会在 CLI 里调试一个 server 才有的问题）**：

============================  ==========  ================================
特性                           SQLite      Postgres
============================  ==========  ================================
会话 / 消息 / 事件 / 用量       ✅          ✅
工具执行记录与幂等              ✅          ✅
pgvector 向量检索              ❌          ✅
``JSONB`` 的索引与操作符        ⚠ 部分      ✅
多进程并发写                    ❌          ✅
============================  ==========  ================================

**建表用 ``create_all`` 而不是迁移**：CLI 的默认库是**内存**的，进程退出即丢 ——
它没有「版本演进」这回事，而引一套迁移会让这个入口重得多。
要持久化到文件或 Postgres 时，应当走迁移那条路（那是 server 的路径）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from foundation.database import Database
from foundation.db import Base, create_engine

__all__ = ["MEMORY_DSN", "open_database", "open_database_from_config", "prepare_schema"]

_log = logging.getLogger(__name__)

#: 内存 SQLite。**注意它是 per-connection 的** ——
#: ``foundation.db.create_engine`` 对它单独用了 ``StaticPool``，
#: 否则「建表」与「查表」会落在两个不同的空库里，表现为「表不存在」，
#: 而建表那句明明刚成功过。
MEMORY_DSN = "sqlite+aiosqlite:///:memory:"


async def prepare_schema(database: Database) -> None:
    """把全部表建出来。

    ⚠ 必须先 import ``repo.models`` —— ``create_all`` 只建**此刻已注册到
    ``Base.metadata``** 的表。漏了注册，报错是「表不存在」，
    而建表那句明明刚刚成功过（这与 Alembic 的 ``env.py`` 是同一个坑）。
    """
    import repo.models  # noqa: F401  注册全部模型

    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def open_database(
    dsn: str | None = None,
    *,
    echo: bool = False,
    create_tables: bool = True,
) -> Database:
    """建一个 SQLite 支撑的执行入口。

    Args:
        dsn: 缺省用 :data:`MEMORY_DSN`（进程退出即丢）。传文件路径即成为持久库。
        echo: SQL 回显，仅排障用。
        create_tables: 是否顺带建表。**文件库上也要建** ——
            它同样没有迁移，第一次跑时表是不存在的。

    Returns:
        ``Database``。调用方负责 ``aclose()``。
    """
    database = Database(create_engine(dsn or MEMORY_DSN, echo=echo))
    if create_tables:
        await prepare_schema(database)
    return database


async def open_database_from_config(
    cfg: Mapping[str, Any] | None = None,
) -> Database:
    """从配置建（``sqlite: {dsn: ...}`` 段，缺省内存）。

    保留这个入口是为了让组合根对「CLI 用哪个存储」有**一个统一的问法**，
    而不必在组合根里判断「有没有配 postgres」。
    """
    data = dict(cfg or {})
    return await open_database(str(data.get("dsn") or MEMORY_DSN))

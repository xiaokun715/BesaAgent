"""SQLite 后端：CLI 的默认存储，也可用作开发期的临时库。

**与 Postgres 用的是同一套代码**：``foundation.database.Database`` 只认引擎，
不认识方言。所以 CLI 与 server 跑的是**同一份仓储实现** ——
这正是「抽象点放在引擎而不是事务类」这个修订（见 ``foundation/database.py``
模块 docstring 的「实现期修订」）换来的收益。

**能力边界（必须知道，否则会在 CLI 里调试一个 server 才有的问题）**：

============================  ==========  ================================
特性                           SQLite      Postgres
============================  ==========  ================================
会话 / 消息 / 事件 / 用量       ✅          ✅
pgvector 向量检索              ❌          ✅
``JSONB`` 的索引与操作符        ⚠ 部分      ✅
多进程并发写                    ❌          ✅
============================  ==========  ================================
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from foundation.database import Database
from foundation.db import create_engine

__all__ = ["MEMORY_DSN", "open_database"]

#: 内存 SQLite。**注意它是 per-connection 的** ——
#: ``foundation.db.create_engine`` 对它单独用了 ``StaticPool``，
#: 否则「建表」与「查表」会落在两个不同的空库里，表现为「表不存在」，
#: 而建表那句明明刚成功过。
MEMORY_DSN = "sqlite+aiosqlite:///:memory:"


async def open_database(
    dsn: str | None = None,
    *,
    echo: bool = False,
) -> Database:
    """建一个 SQLite 支撑的执行入口。

    Args:
        dsn: 缺省用 :data:`MEMORY_DSN`（进程退出即丢）。传文件路径即成为持久库。
        echo: SQL 回显，仅排障用。

    Returns:
        ``Database``。调用方负责 ``aclose()``。
    """
    return Database(create_engine(dsn or MEMORY_DSN, echo=echo))


def open_database_from_config(
    cfg: Mapping[str, Any] | None = None,
) -> Database:
    """从配置建（``sqlite: {dsn: ...}`` 段，缺省内存）。

    保留这个入口是为了让组合根对「CLI 用哪个存储」有**一个统一的问法**，
    而不必在组合根里判断「有没有配 postgres」。
    """
    data = dict(cfg or {})
    return Database(create_engine(str(data.get("dsn") or MEMORY_DSN)))

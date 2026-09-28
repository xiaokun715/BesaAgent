"""SQLAlchemy Base、命名约定与异步引擎/会话工厂（供 Alembic 与运行时共用）。

**谁用**：Alembic 的迁移环境与运行时共用同一份 Base 与引擎装配，避免「迁移看到的表」
和「运行时看到的表」不一致。

**谁不用**：``src/repo`` 的仓储**不自己建引擎** —— 它们接收注入了执行入口的实例
（见 ``foundation/database.py``）。建引擎的权利只在组合根手里。

**为什么命名约定要在这里统一定义**：约束名（primary key / foreign key / unique / index）
若各处不一致，Alembic 的 autogenerate 会反复产生「重命名约束」的**假 diff**，
迁移文件越堆越乱，最后没人敢跑 autogenerate。这是可以一次性避免的长期成本。

**依赖边界**：本模块允许依赖 sqlalchemy / asyncpg，**不 import** 任何
``src/repo`` 或业务模块 —— 它就是「引擎怎么建」这一件事的事实来源。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "create_engine",
    "create_session_factory",
    "dsn_scheme",
]

#: 约束命名约定。**必须只有一份** —— 见模块 docstring 关于「假 diff」的说明。
#:
#: ``ck`` 用 ``%(constraint_name)s``：CHECK 约束没有稳定的列名可拼，
#: 所以要求每个 CHECK 显式起名（不起名时 SQLAlchemy 会报错，这正是我们要的 ——
#: 一个匿名的 CHECK 在后来的迁移里无法被引用，也删不掉）。
NAMING_CONVENTION: Mapping[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的基类。

    **业务模块的表也继承它**（``docs/repo`` 的 ``FR-R-09``）——
    只继承 ``DeclarativeBase`` 而不挂到同一个 ``Base`` 上，Alembic 就看不到那张表，
    而且跨表事务不会成立。
    """

    metadata = MetaData(naming_convention=dict(NAMING_CONVENTION))


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

#: 异步驱动白名单。**刻意是白名单而不是黑名单** ——
#: 同步驱动（``psycopg2`` / 裸 ``sqlite``）写进来会在第一次 await 时才炸，
#: 而那时的报错离这里很远。
_ASYNC_DRIVERS: frozenset[str] = frozenset({"postgresql+asyncpg", "sqlite+aiosqlite"})

#: 内存 SQLite 的 DSN。单独列出是因为它需要 StaticPool（见 :func:`create_engine`）。
_SQLITE_MEMORY = "sqlite+aiosqlite:///:memory:"


def dsn_scheme(dsn: str) -> str:
    """取 DSN 的 ``驱动`` 部分（``postgresql+asyncpg://...`` → ``postgresql+asyncpg``）。"""
    return dsn.split("://", 1)[0].strip().lower() if "://" in dsn else ""


def create_engine(
    dsn: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    timeout_s: float = 30.0,
    echo: bool = False,
) -> AsyncEngine:
    """按 DSN 建**异步**引擎。

    Args:
        dsn: 必须带异步驱动（``postgresql+asyncpg://`` 或 ``sqlite+aiosqlite://``）。
        pool_size: 连接池基线大小。**默认刻意取小** —— 连接是全局共享的稀缺资源，
            本机 PostgreSQL 的 ``max_connections`` 是 100 且多进程共享，
            「每个进程开大池」是典型的局部最优、全局最差。
        max_overflow: 峰值允许额外开的连接数。
        timeout_s: 从池里取连接的等待上限。超时**必须报错而不是无限等** ——
            无限等会让一次调用卡死而不是失败，而卡死更难排障。
        echo: SQL 回显。**默认关**：回显会把参数连敏感值一起打进日志。

    Raises:
        ValueError: DSN 为空、或用了同步驱动。
    """
    scheme = dsn_scheme(dsn)
    if not scheme:
        raise ValueError(
            "postgres.dsn 为空或格式不对（应以 'postgresql+asyncpg://' 开头）"
        )
    if scheme not in _ASYNC_DRIVERS:
        known = ", ".join(sorted(_ASYNC_DRIVERS))
        hint = ""
        if scheme in {"postgresql", "postgres"}:
            hint = "（补上 +asyncpg：postgresql+asyncpg://...）"
        elif scheme == "sqlite":
            hint = "（补上 +aiosqlite：sqlite+aiosqlite://...）"
        raise ValueError(
            f"DSN 需要异步驱动，得到 {scheme!r}；已知：{known}{hint}"
        )

    options: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}

    if scheme == "sqlite+aiosqlite":
        # ⚠ 内存 SQLite 的经典陷阱：默认每个连接**各自**是一个空库，
        # 于是「建表」和「查表」可能落在两个不同的库里，表现为「表不存在」——
        # 而建表那句明明刚成功过。StaticPool 让全部连接共用同一个内存库。
        # 文件 SQLite 不需要这个（大家指向同一个文件）。
        if dsn.rstrip("/") == _SQLITE_MEMORY.rstrip("/"):
            from sqlalchemy.pool import StaticPool

            options["poolclass"] = StaticPool
            options["connect_args"] = {"check_same_thread": False}
        # 文件 SQLite 用默认池即可；它没有 pool_size 的概念
    else:
        options.update(
            pool_size=max(1, int(pool_size)),
            max_overflow=max(0, int(max_overflow)),
            pool_timeout=float(timeout_s),
        )

    return create_async_engine(dsn, **options)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[Any]:
    """建会话工厂。

    ``expire_on_commit=False`` 是**必须的**：默认值会让提交后访问对象属性触发
    一次**隐式 IO**（因为事务已结束，属性被标记过期），而在仓储返回对象之后再访问
    就会炸 ``MissingGreenlet`` —— 报错点离原因很远。交付出去的对象不该依赖会话还活着。
    """
    return async_sessionmaker(engine, expire_on_commit=False)

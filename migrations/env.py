"""Alembic 环境。

**两件事必须与运行时完全一致**，否则会出现「迁移看到的表」与「运行时看到的表」不一致 ——
那是最难查的一类问题（迁移说建好了，程序说列不存在）：

1. **`Base`**：来自 ``foundation/db.py``，与运行时同一个（含同一份约束命名约定）。
   命名约定不一致会让 autogenerate 反复产生「重命名约束」的**假 diff**。
2. **DSN**：来自 ``foundation.settings.load_config()``，不在这里另写一份。

**依赖方向上的一个例外（唯一一处）**：本文件要 import 各 app 的 storage 模型
（如 ``apps.server.storage.postgres.vector``）才能让 Alembic 看到那些表。
这是允许的 —— 迁移是**集成点**，它的职责就是「把全部表都看见」。
除此之外 ``src/`` 不 import ``apps/``。

**为什么 ``alembic.ini`` 是纯 ASCII 的**（这条踩过，写下来别再踩）：
Alembic 用 ``encoding="locale"`` 读那个文件，而中文 Windows 的 locale 是 **cp936**。
里面只要有 UTF-8 的中文字符，alembic 会在启动前就死：
``UnicodeDecodeError: 'gbk' codec can't decode byte ...``。
``PYTHONUTF8=1`` 能绕过，但让一个构建工具依赖环境变量才能跑太脆 ——
所以那份文件保持 ASCII，中文说明放在这里（``.py`` 永远按 UTF-8 读）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from foundation.db import Base
from foundation.settings import load_config

# 让 Alembic 看到全部表定义。**这些 import 是有副作用的**（注册到 Base.metadata），
# 所以不能因为「看着没用到」就删掉。
import repo.usage  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _dsn() -> str:
    """从项目配置取 DSN，并做一次「连的不是别人的库」的粗检。"""
    cfg = load_config()
    dsn = str(cfg.get("postgres.dsn") or "").strip()
    if not dsn:
        raise RuntimeError(
            "postgres.dsn 为空。请在 .env 里设 BESA_POSTGRES_DSN，"
            "或检查 configs/*.yaml。"
        )
    return dsn


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # compare_type / compare_server_default 开着，否则 autogenerate
        # 会漏掉「列类型改了」这类差异，迁移越堆越不准
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：真连库。

    ⚠ **首次部署的前置**：``vector`` 扩展需要超级用户才能创建
    （实测 ``vector.control`` 里没有 ``trusted = true``）。
    扩展**已存在**时，本项目角色跑 ``CREATE EXTENSION IF NOT EXISTS`` 能通过 ——
    所以首次部署要由超级用户先执行一次，详见
    ``docs/repo/架构概要设计-repo.md`` 的 ``RR-6``。
    """
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _dsn()

    engine = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

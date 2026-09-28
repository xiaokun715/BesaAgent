"""PostgreSQL 引擎的构造与启动期健康检查。

**为什么引擎只在 app 层建**：``foundation/db.py`` 定死了「建引擎的权利只在组合根手里」。
``src/repo`` 的仓储接收的是**注入了执行入口的实例**，不是引擎 ——
这样「谁在什么时候连上数据库」这个问题只有一个答案。

**健康检查的定位**：它检查的不是「网络通不通」（那是 ``pool_pre_ping`` 的事），
而是**「连上的是不是那个对的库、那个对的版本」**。这两类问题在运行时的表现
都是「某张表不存在」，而修法完全不同。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from foundation.db import create_engine

__all__ = ["HealthReport", "build_engine", "healthcheck"]


class HealthReport:
    """健康检查的结果。**能读到，不只是抛异常** —— ``doctor`` 要展示它。"""

    __slots__ = ("database", "server_version", "has_vector", "warnings")

    def __init__(
        self,
        *,
        database: str,
        server_version: str,
        has_vector: bool,
        warnings: list[str],
    ) -> None:
        self.database = database
        self.server_version = server_version
        self.has_vector = has_vector
        self.warnings = warnings

    def __repr__(self) -> str:
        return (
            f"HealthReport(database={self.database!r}, pg={self.server_version}, "
            f"vector={self.has_vector}, warnings={self.warnings})"
        )


def build_engine(cfg: Mapping[str, Any]) -> AsyncEngine:
    """按 ``postgres:`` 配置段建引擎。

    Args:
        cfg: ``configs/base.yaml`` 的 ``postgres`` 段。

    Raises:
        ValueError: DSN 缺失或用了同步驱动（由 ``foundation.db.create_engine`` 抛出）。
    """
    dsn = str(cfg.get("dsn") or "").strip()
    pool = cfg.get("pool") or {}
    if not isinstance(pool, Mapping):
        raise ValueError(f"postgres.pool 必须是映射，得到 {type(pool).__name__}")

    return create_engine(
        dsn,
        pool_size=int(pool.get("size", 5)),
        max_overflow=int(pool.get("max_overflow", 5)),
        timeout_s=float(pool.get("timeout_s", 30)),
        echo=bool(cfg.get("echo", False)),
    )


#: 期望存在向量扩展时用的检查。**用 pg_extension 而不是 pg_available_extensions** ——
#: 后者只说明「这台服务器装了 pgvector 的包」，前者才说明「这个库启用它了」。
#: 两者差别很大：本机实测 vector 已装但**新库默认没启用**，必须由迁移去 CREATE EXTENSION。
_HAS_VECTOR = "SELECT 1 FROM pg_extension WHERE extname = 'vector'"

_CURRENT_DB = "SELECT current_database(), current_setting('server_version')"


def _describe_target(engine: AsyncEngine) -> str:
    """把连接目标描述成人能读的一行（**不含密码**）。"""
    url = engine.url
    host = url.host or "?"
    port = url.port or 5432
    return f"{url.database or '?'} @ {host}:{port}"


async def healthcheck(
    engine: AsyncEngine,
    *,
    reject_databases: Mapping[str, Any] | list[str] | None = None,
    require_vector: bool = True,
) -> HealthReport:
    """启动期健康检查。

    检查四件事：**连得上**、**库名对**、**版本对**、**向量扩展在不在**。
    前三件任一不满足直接抛错；第四件按 ``require_vector`` 决定是抛错还是只记警告
    （CLI 的 SQLite 路径与「只做关系存储」的部署不该被向量扩展拦住）。

    Args:
        reject_databases: 明确拒绝连接的库名。默认从配置来
            （``configs/base.yaml`` 的 ``postgres.reject_databases``，默认 ``["besa"]``）。
            **这不是洁癖**：本机 5432 上的 ``besa`` 库属于另一个项目，
            连错过去会让两边的 ``alembic_version`` 互相覆盖，而双方都不会立刻报错。

    Raises:
        RuntimeError: 库名被拒、或要求向量扩展但没启用。
    """
    rejected = {str(x) for x in (reject_databases or [])}

    target = _describe_target(engine)

    try:
        async with engine.connect() as conn:
            database, server_version = (
                await conn.execute(text(_CURRENT_DB))
            ).one()
            has_vector = (
                await conn.execute(text(_HAS_VECTOR))
            ).first() is not None
    except Exception as exc:  # noqa: BLE001
        # 连不上时**必须给出目标信息**。原生报错是 asyncpg 的
        # `ConnectionDoesNotExistError: connection was closed in the middle of operation`
        # —— 它没说连的是哪台、哪个库，也看不出到底是「库不存在」、
        # 「角色不存在」还是「密码错」。本机实测这三种情况报的都是同一句。
        # 排障时第一个要问的就是「你连的是哪个库」，所以这里必须把它补上。
        raise RuntimeError(
            f"连不上 PostgreSQL：{target}\n"
            f"底层错误：{type(exc).__name__}: {exc}\n"
            "常见的三种原因，报错信息分辨不出来，需要逐个排除：\n"
            "  1. 库不存在 —— 建库语句见 docs/repo/需求说明书-repo.md §7\n"
            "  2. 角色不存在或密码错 —— 注意 PostgreSQL 故意不区分这两者\n"
            "  3. 服务没起或端口不对 —— 检查 postgres.dsn"
        ) from exc

    warnings: list[str] = []

    if database in rejected:
        raise RuntimeError(
            f"拒绝连接到 {database!r} 库：它在 postgres.reject_databases 列表里。\n"
            "本机的 `besa` 库属于另一个项目（besa-iv-kb）——两个项目共用会互相覆盖\n"
            "alembic_version，导致双方后续的迁移被静默跳过。\n"
            "本项目应连 `besa_agent`。请检查 BESA_POSTGRES_DSN 或 configs/*.yaml。"
        )

    if require_vector and not has_vector:
        raise RuntimeError(
            f"{database!r} 库没有启用 vector 扩展。\n"
            "迁移里应有 `CREATE EXTENSION IF NOT EXISTS vector`（幂等）。\n"
            "若这个部署不需要向量能力，请把 require_vector 设为 false ——\n"
            "但不要让它静默降级：需要向量却查不到，症状会是「检索总返回空」。"
        )

    if not has_vector:
        warnings.append(
            f"{database!r} 库未启用 vector 扩展；向量相关功能不可用（其余功能正常）"
        )

    return HealthReport(
        database=database,
        server_version=server_version,
        has_vector=has_vector,
        warnings=warnings,
    )

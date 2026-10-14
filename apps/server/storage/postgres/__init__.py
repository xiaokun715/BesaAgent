"""PostgreSQL 后端：引擎构造、执行入口装配、健康检查。

**本包只实现协议，不定义协议**（``NFR-S-01``）：
``Database`` / ``Transaction`` 来自 ``foundation.database``，
``VectorStore`` 来自 ``src/repo``。这里做的事情只有「怎么连上 Postgres」。

**唯一的服务对象是组合根** —— 它建好引擎、拿到 ``Database``，再注入 ``src/repo``。
``src/`` 里出现 ``from apps...`` 的一刻，这个设计就塌了。
"""

from __future__ import annotations

from apps.server.storage.postgres.database import open_database
from apps.server.storage.postgres.engine import HealthReport, build_engine, healthcheck
from apps.server.storage.postgres.vector import (
    EMBEDDING_TABLES,
    SUPPORTED_DIMS,
    DimensionMismatch,
    PgVectorStore,
    UnknownSpace,
    UnsupportedDimension,
    Vector,
    VectorSpaceRow,
    build_vector_store,
    open_vector_store,
    spaces_from_config,
)

__all__ = [
    "EMBEDDING_TABLES",
    "SUPPORTED_DIMS",
    "DimensionMismatch",
    "HealthReport",
    "PgVectorStore",
    "UnknownSpace",
    "UnsupportedDimension",
    "Vector",
    "VectorSpaceRow",
    "build_engine",
    "build_vector_store",
    "healthcheck",
    "open_database",
    "open_vector_store",
    "spaces_from_config",
]

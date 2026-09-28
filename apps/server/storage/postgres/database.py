"""执行入口的装配 —— 把引擎包成 ``Database``，交给组合根。

**为什么不直接在组合根里 ``Database(create_engine(dsn))``**：
那样组合根就要同时知道 DSN 的形状、池参数的含义、健康检查要做哪些事。
把这三件事收在这里之后，组合根只需要::

    db = await open_database(cfg.section("postgres"))

—— 而「Postgres 特有的那些事」不会漏到别的模块去。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from foundation.database import Database

from apps.server.storage.postgres.engine import HealthReport, build_engine, healthcheck

__all__ = ["open_database"]

#: 明确拒绝连接的库名。
#:
#: 默认值是 **跨项目保护**，不是洁癖：本机 5432 上的 ``besa`` 库属于另一个项目
#: （``besa-iv-kb``），已经有 37 张表和它自己的 ``alembic_version``。
#: 连错过去会让两边的迁移历史互相覆盖 —— 而**双方都不会立刻报错**，
#: 症状要等下次跑迁移时才以「本该执行的迁移被跳过」的形式出现。
_DEFAULT_REJECTED = ("besa",)


async def open_database(
    cfg: Mapping[str, Any],
    *,
    require_vector: bool = True,
    check_health: bool = True,
) -> Database:
    """建引擎 → 跑健康检查 → 返回执行入口。

    Args:
        cfg: ``configs/base.yaml`` 的 ``postgres`` 段。
        require_vector: 是否要求 ``vector`` 扩展已启用。
            CLI 的 SQLite 路径不需要它；「只做关系存储」的部署可以关掉。
        check_health: 关掉即跳过健康检查。**只在明确知道为什么关的时候关** ——
            它拦的是「连错库」与「迁移版本不对」这两类**不会立刻报错**的问题。

    Returns:
        ``Database``。**调用方负责在关停时 ``aclose()``**。

    Raises:
        RuntimeError: 健康检查不通过（连错库 / 缺 vector 扩展）。
        ValueError: DSN 缺失或驱动不对。
    """
    engine = build_engine(cfg)

    if not check_health:
        return Database(engine)

    rejected = cfg.get("reject_databases")
    if rejected is None:
        rejected = _DEFAULT_REJECTED

    try:
        report: HealthReport = await healthcheck(
            engine, reject_databases=list(rejected), require_vector=require_vector
        )
    except Exception:
        # 健康检查失败时必须**先把池关掉**再上抛：否则一个启动失败的进程
        # 会一直占着几个连接，而运维看到的是「明明没起来却连着一堆连接」。
        await engine.dispose()
        raise

    for warning in report.warnings:
        import logging

        logging.getLogger(__name__).warning("%s", warning)

    return Database(engine)

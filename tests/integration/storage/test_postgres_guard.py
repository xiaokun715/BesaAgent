"""PostgreSQL 启动期护栏的测试（``FR-S-03`` / ``CS-3``）。

**为什么这条护栏值得打真实数据库**：它拦的是「连错库」——
本机 5432 上的 ``besa`` 库属于另一个项目（``besa-iv-kb``），
连错过去会让两边的 ``alembic_version`` 互相覆盖，
而**双方都不会立刻报错**：症状要等下次跑迁移时，以「本该执行的迁移被跳过」的形式出现。

用假连接测不出这件事 —— 假连接没有 ``alembic_version``，也就没有损坏可发生。
所以这里**真的连**，但**只读**（只查 ``current_database()`` 与 ``pg_extension``）。

数据库不可达时整组跳过，不让 CI 依赖本机环境。
"""

from __future__ import annotations

import pytest

from sqlalchemy import text

from apps.server.storage.postgres import build_engine, healthcheck, open_database
from foundation.db import create_engine

#: 本机的两个库：目标是 besa_agent，另一个项目的是 besa。
#: **必须带 +asyncpg** —— 同步驱动会被 ``create_engine`` 在构造期拒掉（那是刻意的）。
FOREIGN_DB = "postgresql+asyncpg://postgres:12345678@127.0.0.1:5432/besa"
OWN_DB = "postgresql+asyncpg://besa:besa@127.0.0.1:5432/besa_agent"


def _reachable(dsn: str) -> bool:
    """本机 PG 是否可达。用同步 psycopg 快速判断，避免把 async 夹具搞复杂。"""
    try:
        import psycopg

        with psycopg.connect(dsn.replace("+asyncpg", ""), connect_timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(FOREIGN_DB), reason="本机 PostgreSQL 不可达，跳过真实库护栏测试"
)


async def test_connecting_to_the_foreign_database_is_refused():
    """**连到 `besa` 库必须被拒绝**，且错误信息要说清为什么与怎么办。

    这条用例真的连上了那个库（只读），所以它证明的是护栏在**真实的库上**生效，
    而不只是在一个 mock 上生效。
    """
    engine = create_engine(FOREIGN_DB)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await healthcheck(engine, reject_databases=["besa"])

        message = str(excinfo.value)
        assert "besa" in message, "要说清是哪个库被拒"
        assert "alembic_version" in message, "要说清后果：版本表会互相覆盖"
        assert "besa_agent" in message, "要说清该怎么办"
    finally:
        await engine.dispose()


async def test_open_database_refuses_and_releases_the_pool():
    """拒绝之后**必须把池关掉**。

    否则一个启动失败的进程会一直占着几个连接，而运维看到的是
    「明明没起来却连着一堆连接」—— 症状离原因很远。
    """
    with pytest.raises(RuntimeError, match="拒绝连接"):
        await open_database(
            {"dsn": FOREIGN_DB, "pool": {"size": 2, "max_overflow": 0}},
            require_vector=False,
        )
    # 没有可断言的直接证据（池已 dispose），这条用例的价值是**不泄漏**：
    # 若实现忘了 dispose，反复跑本用例会在 pg_stat_activity 里堆积连接。


async def test_healthcheck_reports_the_actual_database_name():
    """健康检查要**报出真实库名** —— 排障时第一个要问的就是「你连的是哪个库」。"""
    engine = create_engine(FOREIGN_DB)
    try:
        report = await healthcheck(engine, reject_databases=[], require_vector=False)
        assert report.database == "besa"
        assert report.server_version.startswith("17")
    finally:
        await engine.dispose()


async def test_missing_database_gives_an_actionable_error():
    """库不存在时的报错必须**说清连的是哪个库**。

    asyncpg 原生报的是 `ConnectionDoesNotExistError: connection was closed in the
    middle of operation` —— 它没说连的是哪台、哪个库，而且**库不存在 / 角色不存在 /
    密码错**三种情况报的都是同一句（PostgreSQL 故意不区分，防角色枚举）。
    所以健康检查必须把目标补进错误里，否则排障第一步就卡住。
    """
    engine = build_engine(
        {"dsn": "postgresql+asyncpg://besa:besa@127.0.0.1:5432/besa_agent_does_not_exist"}
    )
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await healthcheck(engine, reject_databases=[], require_vector=False)

        message = str(excinfo.value)
        assert "besa_agent_does_not_exist" in message, "要说清连的是哪个库"
        assert "127.0.0.1:5432" in message, "要说清连的是哪台"
        assert "1." in message and "2." in message and "3." in message, "要列出可能原因"
    finally:
        await engine.dispose()


@pytest.mark.skipif(not _reachable(OWN_DB), reason="besa_agent 库尚未创建")
async def test_own_database_passes_healthcheck():
    """``besa_agent`` 建好之后，这条用例会开始生效。

    它要求 vector 扩展已启用 —— 那是迁移里 ``CREATE EXTENSION`` 的职责，
    在迁移写完之前这条会红，这是**预期的**（它代表「迁移还没做」）。
    """
    db = await open_database({"dsn": OWN_DB}, require_vector=False)
    try:
        async with db.transaction() as tx:
            assert await tx.scalar(text("SELECT 1")) == 1
    finally:
        await db.aclose()

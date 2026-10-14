"""记忆 ↔ pgvector 的接缝（真实 Postgres + pgvector）。

**为什么这条接缝值得单独打真库**：单元测试里 ``memory`` 接的是 ``FakeVectorStore``，
向量层自己的测试接的是真实 PG —— 两边各自都对，**接起来仍然可能错**。
这里验证的正是那一句：

    MemoryRepo.insert(memory 行) → vector_store.upsert(tx, [VectorRecord])

它同时证明两件事：**向量真的写进了 embeddings_<dim>**，且**与 memory 行同一个事务**。

**前提**：``besa_agent`` 已迁移。不满足时整组跳过。

**不污染共享库**：用唯一的 ``tenant_id`` 作为标记，用完把 memory 行与向量一起删掉。
"""

from __future__ import annotations

import uuid

import pytest

from apps.server.storage.postgres import open_database
from apps.server.storage.postgres.vector import PgVectorStore
from foundation.settings import load_config
from memory.types import MemoryCandidate, MemoryContext
from repo.types import VectorSpace
from tests.unit.memory.conftest import FakeChat, FakeEmbedder, make_service

TEST_DSN = "postgresql://besa:besa@127.0.0.1:5432/besa_agent"
SPACE = "default"
DIM = 1024


def _ready() -> bool:
    try:
        import psycopg

        with psycopg.connect(TEST_DSN, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('memory') IS NOT NULL AND to_regclass('embeddings_1024') IS NOT NULL")
                return bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="besa_agent 不可达或未跑迁移，跳过记忆×向量集成测试"
)


@pytest.fixture
async def db():
    cfg = load_config("dev")
    database = await open_database(cfg.section("postgres"), require_vector=True)
    try:
        yield database
    finally:
        await database.aclose()


@pytest.fixture
def tag() -> str:
    return f"itest-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
async def _cleanup(db, tag):
    yield
    from sqlalchemy import text

    async with db.transaction() as tx:
        # 向量按 owner_id 删：先查出这次写进去的 memory_id
        rows = await tx.execute(
            text("SELECT memory_id FROM memory WHERE tenant_id = :tag"), {"tag": tag}
        )
        ids = [row[0] for row in rows.all()]
        if ids:
            store = PgVectorStore(db)
            await store.delete(tx, space=SPACE, owner_kind="memory", owner_ids=ids)
        await tx.execute(text("DELETE FROM memory WHERE tenant_id = :tag"), {"tag": tag})
        await tx.execute(text("DELETE FROM memory_op_log WHERE tenant_id = :tag"), {"tag": tag})


@pytest.fixture
async def vector_store(db) -> PgVectorStore:
    """注册 ``default`` 空间（与 dev 配置的 dim=1024 一致）。"""
    store = PgVectorStore(db, spaces=[VectorSpace(name=SPACE, dim=DIM, metric="cosine")])
    await store.ensure_spaces()
    return store


def _service(db, vector_store):
    return make_service(
        database=db,
        chat=FakeChat(),
        embedder=FakeEmbedder(dim=DIM),
        enabled=True,
        vector_store=vector_store,
        # 判断闸门关掉：这里要验的是**写入链路**，不是闸门（闸门在单测里覆盖）
        memory_cfg={"gates": {"llm_judge": {"enabled": False}}},
    )


async def test_memory_is_written_to_the_vector_table(db, vector_store, tag):
    """写入一条记忆 → 它的向量**真的**落进了 ``embeddings_1024``。"""
    from sqlalchemy import text

    service = _service(db, vector_store)
    ctx = MemoryContext(scope="session", tenant_id=tag, session_id="s-1")

    outcome = await service.remember_candidate(
        MemoryCandidate(content="本仓库的库名是 besa_agent"), ctx=ctx
    )
    assert outcome.stored

    async with db.transaction() as tx:
        rows = (
            await tx.execute(
                text("SELECT owner_id FROM embeddings_1024 WHERE space = :space AND owner_kind = 'memory'"),
                {"space": SPACE},
            )
        ).all()
    assert [row[0] for row in rows] == [str(outcome.memory_id)]


async def test_recall_uses_the_vector_index(db, vector_store, tag):
    """检索走的是向量那一层 —— 距离非空，且没有向量的记忆不会被向量路径命中。"""
    service = _service(db, vector_store)
    ctx = MemoryContext(scope="session", tenant_id=tag, session_id="s-1")
    await service.remember_candidate(MemoryCandidate(content="本仓库用 pytest 做测试"), ctx=ctx)

    hits = await service.recall("pytest", ctx=ctx)
    assert len(hits) == 1
    assert hits[0].item.content == "本仓库用 pytest 做测试"
    assert hits[0].distance is not None, "应当走通了向量路径（纯结构化时 distance 为 None）"


async def test_vectors_and_memory_share_the_transaction(db, vector_store, tag):
    """**同一个事务**（`FR-R-08` / `FR-M-17`）：业务失败时向量也不留。

    用一个会失败的写入路径来验：往一个**维度不匹配**的空间写，向量层会抛错，
    而整个 ``remember`` 事务随之回滚 —— 库里既没有 memory 行，也没有向量。
    """
    from sqlalchemy import text

    # 故意把空间维度改小，让 upsert 抛 DimensionMismatch
    broken = PgVectorStore(db, spaces=[VectorSpace(name=SPACE, dim=768, metric="cosine")])
    service = make_service(
        database=db,
        chat=FakeChat(),
        embedder=FakeEmbedder(dim=DIM),  # 产出 1024 维
        enabled=True,
        vector_store=broken,
        memory_cfg={"gates": {"llm_judge": {"enabled": False}}},
    )

    ctx = MemoryContext(scope="session", tenant_id=tag, session_id="s-1")
    outcome = await service.remember_candidate(MemoryCandidate(content="这条写不进去"), ctx=ctx)
    assert not outcome.stored, "维度错配应当让整次写入失败（旁路降级，不抛）"

    async with db.transaction() as tx:
        memory_rows = (await tx.execute(text("SELECT 1 FROM memory WHERE tenant_id = :t"), {"t": tag})).all()
        vector_rows = (
            await tx.execute(
                text("SELECT 1 FROM embeddings_1024 WHERE space = :s AND owner_kind = 'memory'"),
                {"s": SPACE},
            )
        ).all()
    assert list(memory_rows) == [], "写入失败后不该留下 memory 行"
    assert list(vector_rows) == [], "也不该留下孤立的向量"

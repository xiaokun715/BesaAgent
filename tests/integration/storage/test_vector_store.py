"""pgvector 实现的端到端验证（真实 Postgres + pgvector）。

**为什么这组必须打真库**：本模块最容易错的一处是**绑定方式** ——
pgvector 定义了 ``double precision[] → vector`` 却**没有** ``text → vector``，
所以「把向量格式化成字符串」在字面量下能跑、在参数化查询下必失败。
这个差别只有真的把那句话发给 pgvector 才会暴露（实测确认，见设计文档 §4.2）。

**前提**：``besa_agent`` 库已建、``alembic upgrade head`` 已跑。
不满足时整组跳过（与 ``test_postgres_guard.py`` 同一纪律）。

**不污染共享库**：每个用例用一个**唯一的空间名**，用完删干净
（向量行 + 空间登记行）。用的是 768/1024 两张真实表，不是临时表 ——
临时表会绕开迁移建出来的索引与约束，那正是要验证的东西。
"""

from __future__ import annotations

import uuid

import pytest

from apps.server.storage.postgres import open_database
from apps.server.storage.postgres.vector import DimensionMismatch, UnknownSpace, PgVectorStore
from foundation.settings import load_config
from repo.embedding import VectorRecord

TEST_DSN = "postgresql://besa:besa@127.0.0.1:5432/besa_agent"


def _ready() -> bool:
    """库可达**且**向量表已建，才算就绪。"""
    try:
        import psycopg

        with psycopg.connect(TEST_DSN, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('embeddings_1024') IS NOT NULL")
                return bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="besa_agent 不可达或未跑迁移（alembic upgrade head），跳过向量集成测试"
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
def space_name() -> str:
    """每个用例一个独一无二的空间名 —— 用完即删，不打扰库里的其它空间。"""
    return f"itest-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
async def _cleanup(db, space_name):
    yield
    async with db.transaction() as tx:
        for table in ("embeddings_768", "embeddings_1024"):
            await tx.execute(
                _text(f"DELETE FROM {table} WHERE space = :space"), {"space": space_name}
            )
        await tx.execute(_text("DELETE FROM vector_spaces WHERE space = :space"), {"space": space_name})


def _text(sql: str):
    from sqlalchemy import text

    return text(sql)


async def _rows(tx, sql: str, params: dict) -> list[dict]:
    """读多列。**必须用 ``execute``** —— ``tx.fetch_all`` 只返回第一列。"""
    result = await tx.execute(_text(sql), params)
    return [dict(row) for row in result.mappings().all()]


async def _store(db, space_name: str, *, dim: int = 1024) -> PgVectorStore:
    store = PgVectorStore(db, spaces=[_space(space_name, dim)])
    await store.ensure_spaces()
    return store


def _space(name: str, dim: int = 1024):
    from repo.types import VectorSpace

    return VectorSpace(name=name, dim=dim, metric="cosine", model_key="itest")


def _vec(dim: int, index: int, *, spread: float = 1.0) -> tuple[float, ...]:
    """构造一个在第 ``index`` 维上突出的单位向量 —— 距离关系一眼可预期。"""
    values = [0.0] * dim
    values[index % dim] = spread
    return tuple(values)


# ---------------------------------------------------------------- 空间
async def test_ensure_spaces_registers_and_is_idempotent(db, space_name):
    store = await _store(db, space_name)
    assert space_name in store.spaces

    # 再跑一次不该报错（ON CONFLICT DO NOTHING）
    await store.ensure_spaces()

    async with db.transaction() as tx:
        rows = await _rows(
            tx, "SELECT dim, metric FROM vector_spaces WHERE space = :space", {"space": space_name}
        )
    assert len(rows) == 1
    assert rows[0]["dim"] == 1024


async def test_ensure_spaces_rejects_dimension_change(db, space_name):
    """已登记的空间维度被改动 → **报错，不覆盖**。覆盖会让已有向量全部失效。"""
    await _store(db, space_name, dim=1024)
    with pytest.raises(DimensionMismatch, match="不会自动覆盖"):
        await _store(db, space_name, dim=768)


async def test_search_on_unknown_space_raises(db, space_name):
    store = PgVectorStore(db, spaces=[])
    async with db.transaction() as tx:
        with pytest.raises(UnknownSpace):
            await store.search(tx, space="never-registered", vector=_vec(1024, 0), top_k=1)


# ---------------------------------------------------------------- 往返
async def test_upsert_then_search_returns_nearest_first(db, space_name):
    store = await _store(db, space_name)
    near, far = _vec(1024, 0), _vec(1024, 500)

    async with db.transaction() as tx:
        written = await store.upsert(
            tx,
            [
                VectorRecord(space=space_name, owner_kind="memory", owner_id="far", embedding=far),
                VectorRecord(space=space_name, owner_kind="memory", owner_id="near", embedding=near),
            ],
        )
    assert written == 2

    async with db.transaction() as tx:
        hits = await store.search(tx, space=space_name, vector=near, top_k=2)

    assert [hit.owner_id for hit in hits] == ["near", "far"]
    assert hits[0].distance < hits[1].distance
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)


async def test_upsert_replaces_the_same_owner(db, space_name):
    """``uniq(space, owner_kind, owner_id)`` 让「换模型」是**替换**而不是并存两条。"""
    store = await _store(db, space_name)

    async with db.transaction() as tx:
        await store.upsert(
            tx, [VectorRecord(space=space_name, owner_kind="memory", owner_id="m-1", embedding=_vec(1024, 0))]
        )
    async with db.transaction() as tx:
        await store.upsert(
            tx,
            [VectorRecord(space=space_name, owner_kind="memory", owner_id="m-1",
                          embedding=_vec(1024, 7), model_key="newer")],
        )

    async with db.transaction() as tx:
        rows = await _rows(
            tx,
            "SELECT model_key FROM embeddings_1024 WHERE space = :space AND owner_id = 'm-1'",
            {"space": space_name},
        )
    assert len(rows) == 1, "同一个 owner 只能有一行向量"
    assert rows[0]["model_key"] == "newer"

    async with db.transaction() as tx:
        hits = await store.search(tx, space=space_name, vector=_vec(1024, 7), top_k=1)
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6), "替换后应当命中新向量"


# ---------------------------------------------------------------- 维度错配
async def test_upsert_rejects_dimension_mismatch(db, space_name):
    """`DV-7`：截断或补齐后的距离没有数学意义，而且**不会报错**。"""
    store = await _store(db, space_name, dim=1024)
    async with db.transaction() as tx:
        with pytest.raises(DimensionMismatch, match="不截断、不补齐"):
            await store.upsert(
                tx,
                [VectorRecord(space=space_name, owner_kind="memory", owner_id="x",
                              embedding=_vec(768, 0))],
            )


async def test_search_rejects_dimension_mismatch(db, space_name):
    store = await _store(db, space_name, dim=1024)
    async with db.transaction() as tx:
        with pytest.raises(DimensionMismatch):
            await store.search(tx, space=space_name, vector=_vec(768, 0), top_k=1)


# ---------------------------------------------------------------- 过滤与删除
async def test_owner_filters_narrow_the_search(db, space_name):
    store = await _store(db, space_name)
    point = _vec(1024, 3)

    async with db.transaction() as tx:
        await store.upsert(
            tx,
            [
                VectorRecord(space=space_name, owner_kind="memory", owner_id="a", embedding=point),
                VectorRecord(space=space_name, owner_kind="document", owner_id="b", embedding=point),
            ],
        )

    async with db.transaction() as tx:
        only_memory = await store.search(tx, space=space_name, vector=point, top_k=5, owner_kind="memory")
    assert [hit.owner_id for hit in only_memory] == ["a"]

    async with db.transaction() as tx:
        narrowed = await store.search(tx, space=space_name, vector=point, top_k=5, owner_ids=["b"])
    assert [hit.owner_id for hit in narrowed] == ["b"]


async def test_delete_removes_the_vectors(db, space_name):
    """硬删除记忆时必须连向量一起删（`FR-M-03`）—— 向量留着 = 内容没删干净。"""
    store = await _store(db, space_name)
    async with db.transaction() as tx:
        await store.upsert(
            tx, [VectorRecord(space=space_name, owner_kind="memory", owner_id="gone", embedding=_vec(1024, 1))]
        )

    async with db.transaction() as tx:
        removed = await store.delete(tx, space=space_name, owner_kind="memory", owner_ids=["gone"])
    assert removed == 1

    async with db.transaction() as tx:
        hits = await store.search(tx, space=space_name, vector=_vec(1024, 1), top_k=5)
    assert hits == ()


async def test_delete_of_missing_owner_is_zero_not_an_error(db, space_name):
    store = await _store(db, space_name)
    async with db.transaction() as tx:
        assert await store.delete(tx, space=space_name, owner_kind="memory", owner_ids=["nope"]) == 0


# ---------------------------------------------------------------- 与业务行同事务
async def test_vectors_can_roll_back_with_the_business_row(db, space_name):
    """**向量与业务行同事务**（`FR-R-08`）：事务回滚，两边都不留。

    这是「向量必须写在同一个 tx 里」唯一能证明的方式 ——
    若 ``VectorStore`` 自己持有连接，这条断言就会失败。
    """
    store = PgVectorStore(db, spaces=[_space(space_name)])

    with pytest.raises(RuntimeError):
        async with db.transaction() as tx:
            await store.upsert(
                tx,
                [VectorRecord(space=space_name, owner_kind="memory", owner_id="rollback", embedding=_vec(1024, 2))],
            )
            raise RuntimeError("故意失败，让整个事务回滚")

    async with db.transaction() as tx:
        rows = await tx.fetch_all(
            _text("SELECT 1 FROM embeddings_1024 WHERE space = :space"), {"space": space_name}
        )
    assert list(rows) == [], "事务回滚后向量不该留下"

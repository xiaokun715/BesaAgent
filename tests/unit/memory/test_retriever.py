"""混合检索（`FR-M-05` / `FR-M-15`）。

**为什么这组值得写**：检索的两条底座条件 —— ``state='active'`` 与 ``scope`` ——
一旦漏掉，测试**不会失败**，只会安静地把过时的、或别的租户的记忆返回。
所以这里直接把这两条钉死在断言里。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from foundation.ids import new_id
from memory.retriever import RetrievalPolicy, Retriever, lexical_overlap
from memory.store import MemoryStore, content_hash
from memory.types import MemoryContext, MemoryItem, MemorySource
from repo.memory import MemoryRepo, MemoryView

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _view(content: str, *, scope: str = "session", tenant_id: str = "", state: str = "active",
          valid_to: datetime | None = None, weight: float = 1.0) -> MemoryView:
    return MemoryView(
        memory_id=str(new_id()),
        kind="fact",
        content=content,
        scope=scope,
        tenant_id=tenant_id,
        state=state,
        content_hash=content_hash(content, scope),
        weight=weight,
        created_at=NOW,
        updated_at=NOW,
        valid_to=valid_to,
    )


def test_lexical_overlap_bounds():
    assert lexical_overlap("", "x") == 0.0
    assert lexical_overlap("pytest 测试框架", "pytest 测试框架") == pytest.approx(1.0)
    assert lexical_overlap("pytest", "完全不相干的内容") == 0.0


def test_merge_threshold_must_be_ge_similarity():
    with pytest.raises(ValueError):
        RetrievalPolicy.from_config(
            {}, {"similarity_threshold": 0.95, "merge_threshold": 0.90}
        )


async def _seed(db, views, *, store_vectors=None, embedder=None):
    async with db.transaction() as tx:
        repo = MemoryRepo(tx, store_vectors)
        for view in views:
            embedding = None
            if embedder is not None and store_vectors is not None:
                vectors = await embedder.embed([view.content])
                embedding = tuple(vectors[0])
            await repo.insert(view, embedding=embedding)


async def test_only_active_is_retrieved(db, embedder):
    await _seed(db, [
        _view("用 pytest 做测试"),
        _view("这条已退场", state="tombstone"),
        _view("这条被取代了", state="superseded"),
    ])
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(embedder, RetrievalPolicy())
        hits = await retriever.retrieve(store, query="pytest 测试", ctx=MemoryContext(scope="session"), now=NOW)
    assert [hit.item.content for hit in hits] == ["用 pytest 做测试"]


async def test_expired_memory_is_not_retrieved(db, embedder):
    await _seed(db, [
        _view("过期的任务状态", valid_to=NOW - timedelta(days=1)),
        _view("仍然有效的事实"),
    ])
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(embedder, RetrievalPolicy())
        hits = await retriever.retrieve(store, query="事实", ctx=MemoryContext(scope="session"), now=NOW)
    assert "过期的任务状态" not in [hit.item.content for hit in hits]


async def test_scope_isolation(db, embedder):
    """跨租户查询必须零命中 —— 这是安全边界，不是数据问题（`NFR-M-03`）。"""
    await _seed(db, [_view("A 租户的秘密", tenant_id="t-1")])
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(embedder, RetrievalPolicy())
        hits = await retriever.retrieve(
            store, query="秘密", ctx=MemoryContext(scope="session", tenant_id="t-2"), now=NOW
        )
    assert hits == ()


async def test_vector_path_marks_distance(db, embedder, vector_store):
    await _seed(db, [_view("用 pytest 做测试"), _view("用 unittest 做测试")],
                store_vectors=vector_store, embedder=embedder)
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx, vector_store))
        retriever = Retriever(embedder, RetrievalPolicy())
        hits = await retriever.retrieve(store, query="pytest", ctx=MemoryContext(scope="session"), now=NOW)
    assert hits and all(hit.distance is not None for hit in hits)


async def test_structured_path_when_no_embedder(db):
    """没有向量能力时退到结构化 + 关键词 —— 这是一条**完整**的路径，不是残废版。"""
    await _seed(db, [_view("用 pytest 做测试")])
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(None, RetrievalPolicy())
        hits = await retriever.retrieve(store, query="pytest", ctx=MemoryContext(scope="session"), now=NOW)
    assert len(hits) == 1
    assert hits[0].distance is None
    assert hits[0].score == pytest.approx(hits[0].lexical)


async def test_embedder_failure_degrades_to_lexical(db, embedder):
    """向量化失败**不抛** —— 退到关键词路径，检索仍然可用。"""
    await _seed(db, [_view("用 pytest 做测试")])
    embedder.fail = True
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(embedder, RetrievalPolicy())
        hits = await retriever.retrieve(store, query="pytest", ctx=MemoryContext(scope="session"), now=NOW)
    assert len(hits) == 1


async def test_max_results_caps_output(db, embedder):
    await _seed(db, [_view(f"事实 {index} 关于 pytest") for index in range(10)])
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(embedder, RetrievalPolicy(max_results=3))
        hits = await retriever.retrieve(store, query="pytest", ctx=MemoryContext(scope="session"), now=NOW)
    assert len(hits) <= 3


async def test_max_bytes_caps_output(db, embedder):
    """超过字节上限时**整条丢弃**，不切半个 —— 但**至少留一条**（否则等于返回空集）。"""
    await _seed(db, [_view("长内容 " + "字" * 400) for _ in range(5)])
    async with db.transaction() as tx:
        store = MemoryStore(MemoryRepo(tx))
        retriever = Retriever(embedder, RetrievalPolicy(max_bytes=80))
        hits = await retriever.retrieve(store, query="内容", ctx=MemoryContext(scope="session"), now=NOW)
    assert len(hits) == 1

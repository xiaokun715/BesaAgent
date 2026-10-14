"""``src/memory`` 单测的公共夹具。

**LLM 与向量都用确定性的假实现**（`NFR-M-05`）：记忆的判定正确性
（闸门分流、四分支、衰减）**不该依赖真模型的输出** —— 那会让测试要么不稳定，
要么只能断言「大概是对的」。

**数据库用内存 SQLite**：``memory`` 的唯一约束、状态过滤、``valid_to`` 时效
这几条，只有真的有一个数据库才测得出来。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest

from foundation.clock import FakeClock
from foundation.database import Database
from foundation.db import Base, create_engine
from memory.decay import DecayPolicy
from memory.extractor import ExtractPolicy, Extractor
from memory.manager import MemoryLimits, MemoryService
from memory.policy import GatePolicy, WriteGate
from memory.reflect import ReflectPolicy, Reflector
from memory.resolver import Resolver
from memory.retriever import RetrievalPolicy, Retriever
from memory.scheduler import MaintenancePolicy
from memory.store import MemoryStore
from repo.memory import MemoryRepo

# **必须模块级 import**：`db` 夹具里的 create_all 只建「此刻已注册」的表。
import repo.memory  # noqa: F401  (E402)


class FakeEmbedder:
    """确定性向量化：把词哈希进固定维度。

    不追求语义质量，只保证**同词多半得高分**，足以驱动「向量路径被走到」的断言。
    """

    def __init__(self, dim: int = 16, *, fail: bool = False) -> None:
        self.dim = dim
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedder 故意失败")
        return tuple(self._vector(text) for text in texts)

    def _vector(self, text: str) -> tuple[float, ...]:
        buckets = [0.0] * self.dim
        for token in text.split():
            index = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16) % self.dim
            buckets[index] += 1.0
        norm = math.sqrt(sum(x * x for x in buckets)) or 1.0
        return tuple(x / norm for x in buckets)


class FakeChat:
    """脚本化的补全。按**系统提示里的标记**分流到不同的预设回答。"""

    def __init__(self) -> None:
        self.judge: Mapping[str, Any] | None = {"should_store": True, "kind": "fact", "confidence": 0.8}
        #: 提取返回的条目
        self.extract_items: Sequence[Mapping[str, Any]] = ()
        #: (新信息, 已有记忆) -> relation
        self.relation_fn = lambda new, old: "unrelated"  # noqa: E731
        self.reflect_items: Sequence[Mapping[str, Any]] = ()
        self.distill_item: Mapping[str, Any] | None = None
        self.fail = False
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        self.calls.append((system[:12], prompt))
        if self.fail:
            raise RuntimeError("chat 故意失败")
        if "判定器" in system:
            return json.dumps(self.judge or {})
        if "从对话里提取" in system:
            return json.dumps({"items": list(self.extract_items)})
        if "判断「新信息」" in system:
            return json.dumps({"relation": self.relation_fn(prompt, "")})
        if "压成一条" in system:
            return json.dumps({"items": [self.distill_item]} if self.distill_item else {"items": []})
        if "归纳出其中" in system:
            return json.dumps({"items": list(self.reflect_items)})
        return "{}"


class FakeVectorStore:
    """向量存储的内存实现。**只给测试用**（真实实现在 apps/server/storage/postgres）。

    它只需要两件事做对：``upsert`` 记住 owner → 向量，``search`` 按余弦距离
    返回最近的若干条。距离的**具体数值**不重要，排序对就行 ——
    而排序正是检索唯一依赖它的地方。
    """

    def __init__(self) -> None:
        self.records: dict[str, tuple[float, ...]] = {}
        self.deleted: list[str] = []

    async def upsert(self, tx, records) -> int:
        for record in records:
            self.records[record.owner_id] = tuple(record.embedding)
        return len(records)

    async def delete(self, tx, *, space, owner_kind, owner_ids) -> int:
        removed = 0
        for owner_id in owner_ids:
            if self.records.pop(owner_id, None) is not None:
                self.deleted.append(owner_id)
                removed += 1
        return removed

    async def search(self, tx, *, space, vector, top_k, owner_kind=None, owner_ids=None):
        from repo.embedding import VectorHit

        allowed = set(owner_ids) if owner_ids else set(self.records)
        scored = [
            (owner_id, _cosine_distance(vector, vec))
            for owner_id, vec in self.records.items()
            if owner_id in allowed
        ]
        scored.sort(key=lambda pair: pair[1])
        return tuple(VectorHit(owner_kind="memory", owner_id=oid, distance=d) for oid, d in scored[:top_k])


def _cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return 1.0 - (dot / norm if norm else 0.0)


@pytest.fixture
async def db():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_767_225_600)  # 2026-01-01T00:00:00Z


@pytest.fixture
def chat() -> FakeChat:
    return FakeChat()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def vector_store() -> FakeVectorStore:
    return FakeVectorStore()


def make_service(
    *,
    database: Database | None,
    chat: FakeChat | None = None,
    embedder: FakeEmbedder | None = None,
    clock: FakeClock | None = None,
    enabled: bool = True,
    memory_cfg: Mapping[str, Any] | None = None,
    vector_store: Any | None = None,
) -> MemoryService:
    """按测试需要装配一个 ``MemoryService``。"""
    cfg = dict(memory_cfg or {})
    limits = MemoryLimits.from_config({"enabled": enabled, **cfg})
    retrieval = RetrievalPolicy.from_config({}, {"similarity_threshold": 0.9, "merge_threshold": 0.96})
    decay = DecayPolicy.from_config(cfg.get("decay"))

    def store_factory(tx: Any) -> MemoryStore:
        return MemoryStore(MemoryRepo(tx, vector_store))

    return MemoryService(
        database=database,
        store_factory=store_factory,
        gate=WriteGate(chat, GatePolicy.from_config(cfg.get("gates") or {})),
        extractor=Extractor(chat, ExtractPolicy.from_config(cfg.get("extract") or {})),
        retriever=Retriever(embedder, retrieval),
        resolver=Resolver(
            chat,
            similarity_threshold=retrieval.similarity_threshold,
            merge_threshold=retrieval.merge_threshold,
        ),
        reflector=Reflector(chat, ReflectPolicy.from_config(cfg.get("reflect"))),
        decay=decay,
        scheduler=MaintenancePolicy.from_config(cfg.get("maintenance")),
        limits=limits,
        embedder=embedder,
        clock=clock,
    )

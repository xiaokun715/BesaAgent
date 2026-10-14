"""冲突与冗余判断 → 四分支（`FR-M-16`）。

**为什么这组值得写**：四个分支里有两个是危险的 —— **合并**会丢信息、
**更新**会把对的说成错的。判错的代价不对称（架构文档 §2.4），
所以每条断言不只验证「落到哪个分支」，还验证**保守方向**：
来源更弱时不许推翻既有事实。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from memory.resolver import Resolver
from memory.types import MemoryCandidate, MemoryItem, MemorySource, RetrievalHit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _item(content: str, *, kind: str = "fact", strength: str = "agent", confidence: float = 0.5,
          created_at: datetime | None = None) -> MemoryItem:
    return MemoryItem(
        memory_id=uuid4(), kind=kind, content=content,
        source=MemorySource(kind=strength), confidence=confidence,
        created_at=created_at or NOW, updated_at=created_at or NOW,
    )


def _hit(item: MemoryItem, score: float) -> RetrievalHit:
    return RetrievalHit(item=item, score=score, distance=1.0 - score)


def _resolver(chat) -> Resolver:
    return Resolver(chat, similarity_threshold=0.90, merge_threshold=0.96)


async def test_no_hits_is_add(chat):
    decision = await _resolver(chat).resolve(_candidate(), [], now=NOW)
    assert decision.resolution == "add"


async def test_low_similarity_is_add(chat):
    chat.relation_fn = lambda new, old: "corroborate"
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("无关的事"), 0.5)], now=NOW)
    assert decision.resolution == "add"


async def test_equivalent_above_merge_threshold_merges(chat):
    chat.relation_fn = lambda new, old: "equivalent"
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("接口超时 30s"), 0.97)], now=NOW)
    assert decision.resolution == "merge"


async def test_equivalent_below_merge_threshold_reinforces(chat):
    """合并比强化更保守：够像但不完全等价时，宁可不并。"""
    chat.relation_fn = lambda new, old: "equivalent"
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("接口超时 30s"), 0.92)], now=NOW)
    assert decision.resolution == "reinforce"


async def test_merge_collects_other_similar_hits(chat):
    chat.relation_fn = lambda new, old: "equivalent"
    hits = [_hit(_item("超时是 30s"), 0.99), _hit(_item("超时 30 秒"), 0.97), _hit(_item("边角"), 0.90)]
    decision = await _resolver(chat).resolve(_candidate(), hits, now=NOW)
    assert decision.resolution == "merge"
    assert len(decision.merged) == 1  # 只有 ≥ merge 阈值的那条被并入


async def test_corroborate_reinforces(chat):
    chat.relation_fn = lambda new, old: "corroborate"
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("用 pytest"), 0.93)], now=NOW)
    assert decision.resolution == "reinforce"


async def test_unrelated_is_add(chat):
    chat.relation_fn = lambda new, old: "unrelated"
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("用 pytest"), 0.93)], now=NOW)
    assert decision.resolution == "add"


async def test_contradiction_with_stronger_source_updates(chat):
    chat.relation_fn = lambda new, old: "contradict"
    old = _item("接口超时是 30s", strength="agent", confidence=0.5)
    strong = MemoryCandidate(
        content="接口超时改成 60s", source=MemorySource(kind="user"), confidence=0.9
    )
    decision = await _resolver(chat).resolve(strong, [_hit(old, 0.95)], now=NOW, confidence=0.9)
    assert decision.resolution == "update"
    assert decision.target is old


async def test_contradiction_with_weaker_source_records_conflict(chat):
    """弱来源不得推翻强来源 —— 只记冲突，不假装知道谁对（`MR-4`）。"""
    chat.relation_fn = lambda new, old: "contradict"
    old = _item("接口超时是 30s", strength="user", confidence=0.95)
    weak = MemoryCandidate(content="接口超时是 5s", source=MemorySource(kind="reflection"), confidence=0.2)
    decision = await _resolver(chat).resolve(weak, [_hit(old, 0.95)], now=NOW, confidence=0.2)
    assert decision.resolution == "conflict"
    assert decision.target is old


async def test_judge_unavailable_high_similarity_reinforces(chat):
    """判定不可用时走保守分支：很像我强化，否则添加 —— 都不新建重复。"""
    chat.fail = True
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("用 pytest"), 0.97)], now=NOW)
    assert decision.resolution == "reinforce"


async def test_judge_unavailable_low_similarity_adds(chat):
    chat.fail = True
    decision = await _resolver(chat).resolve(_candidate(), [_hit(_item("用 pytest"), 0.91)], now=NOW)
    assert decision.resolution == "add"


def _candidate() -> MemoryCandidate:
    return MemoryCandidate(content="接口超时是 30s", source=MemorySource(kind="agent"))

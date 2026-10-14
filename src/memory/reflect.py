"""反思与蒸馏（`FR-M-06` / `FR-M-20`，路线二的产出侧）。

============================  ==================================================
**反思** reflect              从一段记忆轨迹里归纳出**更高层的经验**（``lesson``）
**蒸馏** distill              把一簇**语义冗余**的记忆压成一条更抽象、更稳定的记忆
============================  ==================================================

**产出仍然是记忆**（`MR-9`）：只有落成记忆，它才能被检索、被强化、被衰减 ——
与其它记忆同一套生命周期，不另开一套「报告」。所以这里返回的是
:class:`~memory.types.MemoryCandidate`，**必须再走三道闸门**才入库
（反思也可能产出噪声，甚至越界内容）。

**不得凭空造事实**：反思只能**归纳已存在**的记忆，产出要能回指它的输入。
提示词里明确禁止引入外部知识，且 ``reflected`` 类记忆的来源强度最低
（``SOURCE_STRENGTH['reflection'] = 1``）—— 它是**二手信息**，不该推翻它归纳的一手记忆。

**簇怎么找**：用**相似度连通分量**而不是向量聚类（`MQ-5`）——
确定、可测、不依赖随机种子，与 `NFR-M-06` 的确定性要求一致。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from memory.retriever import lexical_overlap
from memory.types import (
    ChatModel,
    MemoryCandidate,
    MemoryContext,
    MemoryItem,
    MemorySource,
)

__all__ = ["ReflectPolicy", "Reflector", "find_clusters"]

_log = logging.getLogger(__name__)

_REFLECT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        }
    },
    "required": ["items"],
}

_REFLECT_SYSTEM = (
    "下面是一批**已经存在**的记忆。归纳出其中**反复出现、可复用的经验教训**。\n"
    "严格要求：\n"
    "- 只能归纳给定内容，**不得引入任何外部知识或想象**；\n"
    "- 每条经验用一句自足的话表述，且必须能在给定内容里找到依据；\n"
    "- 没有可归纳的规律就返回空数组 —— 不要为了凑数编一条。\n"
    "只输出 JSON。"
)

_DISTILL_SYSTEM = (
    "下面是一簇**语义冗余**的记忆（说法不同、指同一件事）。\n"
    "把它们**压成一条**更抽象、更稳定的记忆：保留共同的事实，不要丢掉任何一条独有的信息。\n"
    "只输出 JSON：{\"items\": [{\"content\": \"...\"}]}，且**只给一条**。"
)


@dataclass(frozen=True)
class ReflectPolicy:
    """反思参数（``memory.reflect`` 段）。"""

    enabled: bool = True
    #: 反思输入的轨迹条数上界（`FR-M-20`）。
    max_trajectory_items: int = 200
    #: 一次反思产出的条数上界。
    max_output_items: int = 5
    #: 只有 ``≥`` 这个条数才值得反思。
    min_trajectory_items: int = 3
    #: 蒸馏时的相似度阈值（连通分量用）。
    cluster_similarity: float = 0.6
    #: 一簇至少这么多条才值得蒸馏。
    min_cluster_size: int = 3
    max_tokens: int = 1_024

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "ReflectPolicy":
        data = dict(cfg or {})
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_trajectory_items=int(data.get("max_trajectory_items", 200)),
            max_output_items=int(data.get("max_output_items", 5)),
            min_trajectory_items=int(data.get("min_trajectory_items", 3)),
            cluster_similarity=float(data.get("cluster_similarity", 0.6)),
            min_cluster_size=int(data.get("min_cluster_size", 3)),
            max_tokens=int(data.get("max_tokens", 1_024)),
        )


class Reflector:
    """反思与蒸馏。**产出候选**，由 ``manager`` 送闸门后入库。"""

    __slots__ = ("_chat", "_policy")

    def __init__(self, chat: ChatModel | None, policy: ReflectPolicy) -> None:
        self._chat = chat
        self._policy = policy

    @property
    def policy(self) -> ReflectPolicy:
        return self._policy

    async def reflect(
        self, items: Sequence[MemoryItem], *, ctx: MemoryContext
    ) -> tuple[MemoryCandidate, ...]:
        """从轨迹里归纳 ``lesson``。**不足 / 失败时返回空，不产半成品。**"""
        policy = self._policy
        if not policy.enabled or self._chat is None:
            return ()
        if len(items) < policy.min_trajectory_items:
            return ()

        sample = list(items)[: policy.max_trajectory_items]
        prompt = "\n".join(f"- [{item.kind}] {item.content}" for item in sample)
        payload = await self._complete(_REFLECT_SYSTEM, prompt, policy.max_tokens)
        if payload is None:
            return ()

        source = MemorySource(kind="reflection", trace_id=ctx.trace_id, caller=ctx.caller)
        candidates: list[MemoryCandidate] = []
        for entry in _items_of(payload):
            if len(candidates) >= policy.max_output_items:
                break
            content = str(entry.get("content") or "").strip() if isinstance(entry, Mapping) else ""
            if content:
                # 反思产出固定为 lesson，且来源强度最低（二手信息）。
                candidates.append(MemoryCandidate(content=content, source=source, kind_hint="lesson"))
        return tuple(candidates)

    async def distill(
        self, cluster: Sequence[MemoryItem], *, ctx: MemoryContext
    ) -> MemoryCandidate | None:
        """把一簇冗余记忆压成一条。**簇太小则不蒸馏**（避免无谓的 LLM 调用）。"""
        policy = self._policy
        if not policy.enabled or self._chat is None:
            return None
        if len(cluster) < policy.min_cluster_size:
            return None

        prompt = "\n".join(f"- {item.content}" for item in cluster)
        payload = await self._complete(_DISTILL_SYSTEM, prompt, policy.max_tokens)
        if payload is None:
            return None
        entries = _items_of(payload)
        if not entries:
            return None
        entry = entries[0]
        content = str(entry.get("content") or "").strip() if isinstance(entry, Mapping) else ""
        if not content:
            return None
        source = MemorySource(kind="reflection", trace_id=ctx.trace_id, caller=ctx.caller)
        # 蒸馏保留簇里最强的那个种类（fact 的抽象仍是 fact）。
        kind = cluster[0].kind
        return MemoryCandidate(content=content, source=source, kind_hint=kind)

    async def _complete(self, system: str, prompt: str, max_tokens: int) -> Any | None:
        try:
            raw = await self._chat.complete(
                system=system, prompt=prompt, json_schema=_REFLECT_SCHEMA, max_tokens=max_tokens
            )
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆反思/蒸馏失败，本轮不产出：%s", exc)
            return None


def find_clusters(
    items: Sequence[MemoryItem], *, threshold: float, min_size: int
) -> list[list[MemoryItem]]:
    """把语义冗余的记忆聚成簇（**相似度连通分量**）。

    并查集：两两相似度 ≥ ``threshold`` 就合并。返回的簇按大小降序 ——
    **大的冗余簇更值得蒸馏**。这是确定性的，给定输入结果唯一（`NFR-M-06`）。
    """
    n = len(items)
    if n < min_size:
        return []

    parent = list(range(n))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[root_r] = root_l

    for i in range(n):
        for j in range(i + 1, n):
            if lexical_overlap(items[i].content, items[j].content) >= threshold:
                union(i, j)

    groups: dict[int, list[MemoryItem]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)

    clusters = [members for members in groups.values() if len(members) >= min_size]
    clusters.sort(key=len, reverse=True)
    return clusters


def _items_of(payload: Any) -> Sequence[Any]:
    if isinstance(payload, Mapping):
        items = payload.get("items")
        return items if isinstance(items, Sequence) and not isinstance(items, (str, bytes)) else ()
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return payload
    return ()

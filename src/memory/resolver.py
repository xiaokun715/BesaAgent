"""冲突与冗余判断 → **四分支**（`FR-M-16`，路线一的核心判定）。

检索到「对手」之后，必须回答一个问题：**它是同义、是佐证、还是矛盾？**
答案落到四个动作：

============  ==============================  ==========================================
关系           判据                             动作
============  ==============================  ==========================================
相同          语义等价 + 相似度 ≥ merge         **合并**（证据相加，被并入者留痕）
高度相似      同义/佐证，不矛盾                **强化**（不新建）
矛盾          指向冲突，且新来源更强/更新      **更新**（新版本取代旧版本）
无匹配        检索未命中 / 语义无关            **添加**
============  ==============================  ==========================================

**保守方向**（与工具幂等同一条纪律，架构文档 §2.4）：

    漏合并是浪费（多一条），错合并是丢信息（少一条且无人知道）。
    错更新是污染（把对的说成错的），漏更新是滞后（还留旧的）。

所以：**合并阈值 ≥ 强化阈值**（配置层 `C-M-4` 强制）；**更新要求来源更强或更新**，
否则只记冲突、不推翻既有事实（`MR-4`）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from memory.types import (
    ChatModel,
    MemoryCandidate,
    MemoryItem,
    RetrievalHit,
    Resolution,
)

__all__ = ["ResolveDecision", "Resolver"]

_log = logging.getLogger(__name__)

_RELATION_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["equivalent", "corroborate", "contradict", "unrelated"],
        },
        "reason": {"type": "string"},
    },
    "required": ["relation"],
}

_RELATION_SYSTEM = (
    "判断「新信息」与「已有记忆」的关系，只回答四选一：\n"
    "- equivalent：语义等价，只是措辞不同\n"
    "- corroborate：同义/进一步佐证，不矛盾\n"
    "- contradict：两者不能同时成立\n"
    "- unrelated：只是话题相近，实际不相干\n"
    "拿不准时选 unrelated（宁可不合并，不可错合并）。只输出 JSON。"
)


@dataclass(frozen=True)
class ResolveDecision:
    """一次冲突判定的结论。``target`` 是被作用的既有记忆（如果有）。"""

    resolution: Resolution
    target: MemoryItem | None = None
    #: 合并时，被一并并入 target 的其余记忆（相似度也够高那些）。
    merged: tuple[MemoryItem, ...] = ()
    reason: str = ""
    confidence: float | None = None
    relations: Mapping[str, str] = field(default_factory=dict)


class Resolver:
    """四分支判定器。**判定结论与理由一起返回**，理由要落进操作轨迹。"""

    __slots__ = ("_chat", "_similarity", "_merge")

    def __init__(
        self,
        chat: ChatModel | None,
        *,
        similarity_threshold: float,
        merge_threshold: float,
    ) -> None:
        self._chat = chat
        self._similarity = similarity_threshold
        self._merge = merge_threshold

    async def resolve(
        self,
        candidate: MemoryCandidate,
        hits: Sequence[RetrievalHit],
        *,
        now: datetime,
        confidence: float = 0.5,
    ) -> ResolveDecision:
        """判定候选该以哪种方式落到既有记忆上。"""
        if not hits:
            return ResolveDecision("add", reason="检索未命中任何记忆，作为新条目添加")

        top = hits[0]
        if top.score < self._similarity:
            return ResolveDecision(
                "add",
                reason=(
                    f"最高相关度 {top.score:.3f} < 强化阈值 {self._similarity}，"
                    "与既有记忆不够像，作为新条目添加"
                ),
            )

        relation = await self._relation(candidate.content, top.item.content)

        if relation is None:
            # LLM 判定不可用 —— 走**保守分支**：很像我强化，否则添加。
            # 「添加永远安全」（架构文档 §2.4），而强化不会新建、不丢数据。
            if top.score >= self._merge:
                return ResolveDecision(
                    "reinforce",
                    target=top.item,
                    reason="判定不可用，退到保守分支：相似度已达合并阈值，按强化处理（不新建）",
                )
            return ResolveDecision(
                "add",
                reason="判定不可用，且相似度未达合并阈值，按添加处理",
            )

        if relation == "unrelated":
            return ResolveDecision("add", reason=f"与最相近记忆语义无关：{top.item.content[:40]!r}")

        if relation == "corroborate":
            return ResolveDecision("reinforce", target=top.item, reason="同义佐证，强化既有记忆")

        if relation == "equivalent":
            if top.score >= self._merge:
                others = tuple(
                    hit.item for hit in hits[1:] if hit.score >= self._merge
                )
                return ResolveDecision(
                    "merge",
                    target=top.item,
                    merged=others,
                    reason=f"语义等价且相关度 {top.score:.3f} ≥ 合并阈值 {self._merge}，合并",
                )
            return ResolveDecision(
                "reinforce",
                target=top.item,
                reason="语义等价但未达合并阈值，按强化处理（合并更保守，宁可不并）",
            )

        # relation == "contradict"
        if _candidate_is_stronger(candidate, confidence, top.item, now):
            return ResolveDecision(
                "update",
                target=top.item,
                reason=(
                    f"与 {top.item.content[:40]!r} 矛盾，且新信息来源更强/更新，"
                    "产生新版本取代旧版本"
                ),
            )
        return ResolveDecision(
            "conflict",
            target=top.item,
            reason=(
                f"与 {top.item.content[:40]!r} 矛盾，但**新信息来源更弱** —— "
                "不推翻既有事实，只记冲突待人工/后续证据裁决"
            ),
        )

    async def _relation(self, new_text: str, old_text: str) -> str | None:
        """一次 LLM 判定。失败返回 ``None``（由调用方走保守分支）。"""
        if self._chat is None:
            return None
        prompt = f"新信息：{new_text}\n\n已有记忆：{old_text}"
        try:
            raw = await self._chat.complete(
                system=_RELATION_SYSTEM, prompt=prompt, json_schema=_RELATION_SCHEMA, max_tokens=128
            )
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆冲突判定不可用，走保守分支：%s", exc)
            return None
        if not isinstance(data, Mapping):
            return None
        relation = data.get("relation")
        if relation not in {"equivalent", "corroborate", "contradict", "unrelated"}:
            return None
        return relation


def _candidate_is_stronger(
    candidate: MemoryCandidate, confidence: float, old: MemoryItem, now: datetime
) -> bool:
    """新候选的来源是否强于旧记忆（`FR-M-16`）。

    比较维度与 :meth:`MemoryItem.strength_key` 一致：**来源强度 → 置信度 → 时间**。
    **相等也算强** —— 同样可信时按「更新的为准」。
    """
    new_key = (candidate.source.strength, confidence, now.isoformat())
    return new_key >= old.strength_key()

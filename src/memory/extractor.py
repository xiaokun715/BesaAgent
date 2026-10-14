"""从对话里提取**记忆点**（`FR-M-14`，路线一的第一步）。

产出是 :class:`~memory.types.MemoryCandidate`（**候选**，不是记忆）——
它必须再过三道闸门与冲突判定，才有机会落库。

**为什么产出候选而不是直接给 :class:`~memory.types.MemoryItem`**：
记忆点里没有「这条该不该长期保存」的判定结果 —— 那是闸门的事。
把两件事分开，是为了避免「提取模型既写内容又给自己打分」：
它可以把一句寒暄提取得很"像一条事实"，但闸门②会把它判成噪声。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from memory.types import (
    MEMORY_KINDS,
    ChatModel,
    MemoryCandidate,
    MemoryContext,
    MemorySource,
    MemoryTurn,
)

__all__ = ["ExtractPolicy", "Extractor", "render_conversation"]

_log = logging.getLogger(__name__)

_EXTRACT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "kind": {"type": "string", "enum": list(MEMORY_KINDS)},
                },
                "required": ["content"],
            },
        }
    },
    "required": ["items"],
}

_EXTRACT_SYSTEM = (
    "从对话里提取**值得长期记住**的结论。只提取四类：\n"
    "- fact：稳定事实（长期成立、可验证）\n"
    "- preference：用户偏好（对做法/风格的稳定倾向）\n"
    "- task_state：任务状态（当前进度，会过期）\n"
    "- lesson：经验教训\n"
    "**不要**提取：寒暄与情绪、未被证实的猜测、转瞬即变的信息（时间/编号/一次性参数）。\n"
    "每条用一句**自足**的话表述 —— 脱离这段对话也能读懂。\n"
    "宁可少提，不要凑数。只输出 JSON。"
)


@dataclass(frozen=True)
class ExtractPolicy:
    """提取参数（``memory.write.extract`` 段）。"""

    enabled: bool = True
    #: 一次提取的条数上界。**没有它，一段长对话能灌进几十条噪声。**
    max_items_per_call: int = 5
    #: 输入预算（按字符近似 token）。
    max_input_chars: int = 8_000
    #: 单条候选的长度上界（超过就丢弃，闸门①也会再挡一次）。
    max_item_chars: int = 2_000
    max_tokens: int = 1_024

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "ExtractPolicy":
        data = dict(cfg or {})
        policy = cls(
            enabled=bool(data.get("enabled", True)),
            max_items_per_call=int(data.get("max_items_per_call", 5)),
            max_input_chars=int(data.get("max_input_chars", 8_000)),
            max_item_chars=int(data.get("max_item_chars", 2_000)),
            max_tokens=int(data.get("max_tokens", 1_024)),
        )
        if policy.max_items_per_call <= 0:
            raise ValueError("memory.write.extract.max_items_per_call 必须为正")
        return policy


class Extractor:
    """把一段对话变成若干候选记忆点。"""

    __slots__ = ("_chat", "_policy")

    def __init__(self, chat: ChatModel | None, policy: ExtractPolicy) -> None:
        self._chat = chat
        self._policy = policy

    @property
    def policy(self) -> ExtractPolicy:
        return self._policy

    async def extract(
        self,
        conversation: Sequence[MemoryTurn] | str,
        *,
        source: MemorySource,
        ctx: MemoryContext,
        limit: int | None = None,
    ) -> tuple[MemoryCandidate, ...]:
        """提取候选。**失败返回空元组，不抛** —— 记忆是旁路（`NFR-M-07`）。"""
        policy = self._policy
        if not policy.enabled or self._chat is None:
            return ()

        text = conversation if isinstance(conversation, str) else render_conversation(conversation)
        text = text[: policy.max_input_chars]
        if not text.strip():
            return ()

        try:
            raw = await self._chat.complete(
                system=_EXTRACT_SYSTEM,
                prompt=text,
                json_schema=_EXTRACT_SCHEMA,
                max_tokens=policy.max_tokens,
            )
            payload = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆提取失败，本次不产出候选：%s", exc)
            return ()

        items = _items_of(payload)
        cap = min(limit or policy.max_items_per_call, policy.max_items_per_call)

        candidates: list[MemoryCandidate] = []
        for entry in items:
            if len(candidates) >= cap:
                break
            if not isinstance(entry, Mapping):
                continue
            content = str(entry.get("content") or "").strip()
            if not content or len(content) > policy.max_item_chars:
                continue
            kind = entry.get("kind")
            candidates.append(
                MemoryCandidate(
                    content=content,
                    source=source,
                    kind_hint=kind if kind in MEMORY_KINDS else None,
                )
            )
        return tuple(candidates)


def render_conversation(turns: Sequence[MemoryTurn]) -> str:
    """把若干轮对话渲染成一段文本，喂给提取提示。

    **带 role 前缀**：谁是用户说的、谁是模型说的，直接决定一条信息的可信度
    （用户明说的偏好 ≠ 模型自己的一次推测）。
    """
    lines = [f"{turn.role}: {turn.content}" for turn in turns if turn.content.strip()]
    return "\n".join(lines)


def _items_of(payload: Any) -> Sequence[Any]:
    """兼容两种返回：``{"items": [...]}`` 与裸数组。"""
    if isinstance(payload, Mapping):
        items = payload.get("items")
        return items if isinstance(items, Sequence) and not isinstance(items, (str, bytes)) else ()
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        return payload
    return ()

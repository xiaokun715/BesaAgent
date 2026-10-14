"""摘要器：把一段消息压成一段摘要（`FR-C-03` 的 L2/L3）。

**走的是 ``ChatModel`` 协议，不是 ``Gateway``** —— 与 ``src/memory`` 同一条纪律：
本模块只认协议，实现（把 ``Gateway.chat`` 包成这个形状）由组合根给。
两边形状相同，于是**同一个适配器同时喂给记忆与上下文**。

**失败返回 ``None``，不抛**（`NFR-C-07`）：压缩是**优化**，
不该变成新的故障点 —— 让一次摘要失败把整轮对话打挂，是拿优化换可用性。
"""

from __future__ import annotations

import logging

from context.compact.types import CompactUnit
from context.types import ChatModel, CompressionLevel, ContextBlock, Segment

from context.compact.types import summary_block

__all__ = ["Summarizer"]

_log = logging.getLogger(__name__)

_SYSTEM = (
    "你在压缩一段**已经发生过**的对话历史，供后续轮次参考。\n"
    "要求：\n"
    "- 保留：关键决策与结论、未决问题、约束与约定、专有名词与标识符、失败与踩过的坑；\n"
    "- 丢弃：寒暄、重复内容、可以被推断的过程细节；\n"
    "- **不得编造**原文没有的信息，不得补充你的常识；\n"
    "- 用第三人称陈述，不要写成对话；\n"
    "- 只输出摘要正文，不要任何前后缀。"
)

_DIGEST_SYSTEM = (
    "下面是一组**已经被压缩过一次**的摘要。把它们进一步压成**要点列表**。\n"
    "要求：\n"
    "- 只保留仍然影响后续决策的结论与约束；\n"
    "- **不得编造**；\n"
    "- 用短句，一条一行，以 '- ' 开头；\n"
    "- 只输出要点，不要任何前后缀。"
)


class Summarizer:
    """把消息压成一段摘要。``chat`` 为 ``None`` 时**不可用**（返回 ``None``）。"""

    __slots__ = ("_chat", "_alias", "_max_tokens", "_prompt_version")

    def __init__(
        self,
        chat: ChatModel | None,
        *,
        alias: str = "runtime.default",
        max_tokens: int = 512,
        prompt_version: str = "v1",
    ) -> None:
        self._chat = chat
        self._alias = alias
        self._max_tokens = max_tokens
        self._prompt_version = prompt_version

    @property
    def available(self) -> bool:
        return self._chat is not None

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    async def summarize(
        self,
        unit: CompactUnit,
        *,
        level: CompressionLevel,
        segment: Segment,
        reason: str = "",
    ) -> ContextBlock | None:
        """把一段消息压成摘要块。**失败返回 ``None``。**"""
        if self._chat is None or not unit.messages:
            return None

        prompt = _render(unit)
        system = _DIGEST_SYSTEM if level == "L3" else _SYSTEM
        try:
            text = await self._chat.complete(
                system=system,
                prompt=prompt,
                max_tokens=self._max_tokens,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 — 压缩失败要降级，不能上抛
            _log.warning("上下文压缩失败（会降级到上一级）：%s", exc)
            return None

        text = (text or "").strip()
        if not text:
            _log.warning("上下文压缩返回了空摘要（会降级到上一级）")
            return None

        return summary_block(
            text,
            segment=segment,
            level=level,
            message_ids=unit.message_ids,
            note=reason,
        )


def _render(unit: CompactUnit) -> str:
    """把一段消息渲染成提示词。

    **带 role 前缀**：谁说的直接决定一条信息的可信度（用户明说的约束 ≠ 模型的一次推测）。
    """
    lines: list[str] = []
    for message in unit.messages:
        marker = ""
        if message.role == "tool" and message.tool_call_id:
            marker = f"(工具结果 {message.tool_call_id[:8]})"
        lines.append(f"[{message.role}]{marker}: {message.content}")
    return "\n".join(lines)

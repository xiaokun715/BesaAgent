"""多级压缩自己的类型。**压缩是独立文件夹，这些类型也归它**（`MC-15`）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from context.types import (
    CompressionLevel,
    ContextBlock,
    ContextMessage,
    LedgerEntry,
    Provenance,
)

__all__ = ["CompactResult", "CompactUnit", "blocks_from_messages", "summary_block"]


@dataclass(frozen=True)
class CompactUnit:
    """一段待压缩的内容（按「轮」切出来的中段）。"""

    messages: tuple[ContextMessage, ...]
    #: 这一段的标识，进缓存键（取内容指纹而不是消息 id —— 消息被编辑后应重压）
    fingerprint: str = ""

    @property
    def message_ids(self) -> tuple[str, ...]:
        return tuple(m.message_id for m in self.messages if m.message_id)

    @property
    def tokens(self) -> int:
        from gateway.tokens import estimate_text_tokens

        return sum(estimate_text_tokens(m.content) for m in self.messages)


@dataclass(frozen=True)
class CompactResult:
    """一次压缩的产出。**账本与它同行** —— 没有账本就没有"当时有什么"的后路。"""

    blocks: tuple[ContextBlock, ...]
    entries: tuple[LedgerEntry, ...] = ()
    level_used: CompressionLevel = "L0"
    llm_calls: int = 0
    cached_hits: int = 0
    #: 到 ``max_level`` 仍放不下 —— **如实上报**，不静默截断（`FR-C-13`）
    insufficient: bool = False
    note: str = ""

    @property
    def compacted(self) -> bool:
        return self.level_used != "L0"


def blocks_from_messages(
    messages: tuple[ContextMessage, ...], *, segment: str, level: CompressionLevel
) -> tuple[ContextBlock, ...]:
    """把原文消息包成块。``message`` 字段带着原消息，组装时按它原样还原 ——
    工具调用与结果必须保留它们的 id 才能配对（`FR-C-14`）。"""
    return tuple(
        ContextBlock(
            content=message.content,
            role=message.role,
            provenance=Provenance(
                segment=segment,  # type: ignore[arg-type]
                level=level,
                message_ids=(message.message_id,) if message.message_id else (),
            ),
            message=message,
        )
        for message in messages
    )


def summary_block(
    text: str,
    *,
    segment: str,
    level: CompressionLevel,
    message_ids: tuple[str, ...],
    note: str = "",
    extra: dict[str, Any] | None = None,
) -> ContextBlock:
    """把一段摘要包成块。

    **角色是 ``user`` 而不是 ``assistant``**（`MC-16`）：摘要通常紧跟着最近几轮原文，
    而原文里多半已经有 assistant —— 若摘要是 assistant，就会出现连续两条同角色，
    部分厂商会直接 400。用 ``user`` 并把语义放进**结构化标记**（``to_model_text``），
    既避开了这个坑，也不让模型误以为那是它自己说过的。
    """
    return ContextBlock(
        content=text,
        role="user",
        provenance=Provenance(
            segment=segment,  # type: ignore[arg-type]
            level=level,
            message_ids=message_ids,
            note=note,
        ),
        message=None,
    )

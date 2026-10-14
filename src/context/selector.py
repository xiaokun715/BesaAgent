"""在预算内**挑选**要带的内容，以及**跨段去重**（`FR-C-11` / `FR-C-13`）。

两件事都属于"装配前最后一道"：

- :func:`dedupe`：同一条信息可能同时出现在窗口原文、会话摘要、长期记忆里 ——
  注入两次是**双重浪费**（白占预算 + 让模型以为是两件事）；
- :func:`fit`：按预算给各段的上限裁剪，**整块保留或整块丢弃**，
  不切半个（半截摘要比没有摘要更误导）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from context.budget import estimate_tokens
from context.types import BudgetPlan, ContextBlock, Segment

__all__ = ["ASSEMBLY_ORDER", "dedupe", "fit"]

#: 组装顺序（**历史从旧到新**）。
#:
#: 长期记忆放在最前不是因为"它最老"，而是因为它是**背景** ——
#: 模型先看到"这个人/这个项目一贯是什么样"，再看到"这次发生了什么"。
#: system 与当前输入不在这里：前者由 builder 置顶，后者永远在最后。
ASSEMBLY_ORDER: tuple[Segment, ...] = (
    "long_term",
    "session_summary",
    "transition",
    "window",
)

#: 重复时**保留哪一段**。越靠前保真度越高。
#:
#: 顺序是刻意的：原文 > 结构化记忆条目 > 压缩摘要。
#: 判断依据是**保真度**而不是"哪个更结构化" —— 摘要是有损的，
#: 当它和原文撞在一起时，留下的必须是原文。
_DEDUPE_KEEP_ORDER: tuple[Segment, ...] = (
    "window",
    "transition",
    "long_term",
    "session_summary",
)

#: 短于这个长度的块**不参与去重**。
#:
#: 「好的」「收到」这类短句撞车太容易，而**错去重是丢信息**（`MC-10`）。
#: 宁可漏去几条短的（浪费几十 token），不可错删一条短指令。
_DEDUPE_MIN_CHARS = 40


def dedupe(
    blocks: Mapping[Segment, Sequence[ContextBlock]], *, enabled: bool = True
) -> tuple[dict[Segment, tuple[ContextBlock, ...]], tuple[str, ...]]:
    """跨段去重。

    Returns:
        ``(去重后的块, 被丢掉的描述)`` —— 后者要进账本（`NFR-C-08`）：
        「为什么这条记忆没进来」必须答得上来。
    """
    result: dict[Segment, tuple[ContextBlock, ...]] = {
        segment: tuple(items) for segment, items in blocks.items()
    }
    if not enabled:
        return result, ()

    seen: set[str] = set()
    dropped: list[str] = []

    for segment in _DEDUPE_KEEP_ORDER:
        kept: list[ContextBlock] = []
        for block in result.get(segment, ()):
            key = _normalize(block.content)
            if len(key) >= _DEDUPE_MIN_CHARS and key in seen:
                dropped.append(f"{segment}:{block.content[:48]}")
                continue
            if len(key) >= _DEDUPE_MIN_CHARS:
                seen.add(key)
            kept.append(block)
        result[segment] = tuple(kept)

    return result, tuple(dropped)


def fit(
    blocks: Mapping[Segment, Sequence[ContextBlock]], plan: BudgetPlan
) -> tuple[dict[Segment, tuple[ContextBlock, ...]], tuple[str, ...]]:
    """按 ``plan.used`` 给各段的上限裁剪。

    **整块保留或整块丢弃**（`FR-C-13`）：把一段摘要切一半交给模型，
    比不交给它更糟 —— 它会以为那就是全部。
    """
    kept: dict[Segment, tuple[ContextBlock, ...]] = {}
    notes: list[str] = []

    for segment, items in blocks.items():
        quota = int(plan.used.get(segment, 0))
        taken: list[ContextBlock] = []
        used = 0
        for block in items:
            cost = estimate_tokens(block.content)
            if used + cost <= quota:
                taken.append(block)
                used += cost
            else:
                notes.append(f"{segment} 超出配额，丢弃 {cost} token 的块")
        kept[segment] = tuple(taken)

    return kept, tuple(notes)


def _normalize(text: str) -> str:
    """归一化后再比 —— 只归一空白与大小写，**不做语义归一化**。

    语义等价不在这里判：那是记忆层的 ``resolver`` 用 LLM 做的事，
    而在这里放宽判据会把"话题相近"误判成"重复"（**错去重是丢信息**）。
    """
    return " ".join(text.split()).casefold()

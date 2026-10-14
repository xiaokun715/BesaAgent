"""多级压缩的**级联**（`FR-C-03` / `FR-C-04`）。

```
L0 原文  →  L1 去噪（零 LLM）  →  L2 摘要（一次 LLM）  →  L3 要点（一次 LLM）
```

三条约束决定它的形状：

1. **按需升级，不是一次压到底**：能 L1 解决就绝不调模型。L1 是唯一
   **零成本**且**不丢语义**的一级，很多"差一点点"的情况到它就够了。
2. **只压中段**：尾部 ``keep_recent_turns`` 轮是模型的"工作记忆"，**不参与压缩**（`MC-2`）。
   压它等于让模型忘记自己正在做什么。
3. **到顶仍不够就如实上报**，绝不静默截断（`FR-C-13`）——
   静默截断是本模块最不能出现的一种失败。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Literal

from foundation.clock import Clock

from context.compact.boundaries import by_step, by_turn, fingerprint, flatten, split_tail
from context.compact.cache import CompactCache
from context.compact.ledger import Ledger
from context.compact.policy import CompactPolicy
from context.compact.summarizer import Summarizer
from context.compact.types import CompactResult, CompactUnit, blocks_from_messages, summary_block
from context.types import CompressionLevel, ContextBlock, ContextMessage, Segment

from gateway.tokens import estimate_text_tokens

__all__ = ["Compactor", "denoise"]

_log = logging.getLogger(__name__)

#: 把 3 个以上连续换行压成 2 个。**刻意保守** ——
#: 把空白全部折叠会把代码块与表格压成一坨，那不是"去噪"而是"破坏内容"。
_BLANK_RUN = re.compile(r"\n{3,}")


def denoise(
    messages: Sequence[ContextMessage], *, max_tool_chars: int
) -> tuple[ContextMessage, ...]:
    """L1：去噪 + 按上限截断工具输出。**零 LLM 成本，近无损。**

    **不删除任何消息**：删一条 tool 结果会把配对切断（`FR-C-14`）。
    截断可以 —— 但它**必须留下标记**，否则读的人（和模型）会以为那就是全部。
    """
    cleaned: list[ContextMessage] = []
    for message in messages:
        content = _BLANK_RUN.sub("\n\n", message.content).strip()
        if message.role == "tool" and len(content) > max_tool_chars:
            original = len(content)
            content = (
                content[:max_tool_chars]
                + f"\n…[L1 截断：原文 {original} 字符，这里只保留前 {max_tool_chars}]"
            )
        cleaned.append(replace(message, content=content) if content != message.content else message)
    return tuple(cleaned)


class Compactor:
    """级联压缩器。**两条策略共用同一个对象**（`MC-15`）。

    ⚠ **账本是"每次调用一个"**，不是挂在实例上的：挂在实例上会把整个进程生命周期
    的压缩都累积进来，于是「这次请求压了几次」永远读成「至今一共压了几次」——
    而那种数字在排障时比没有还坏（它看起来是对的）。
    """

    __slots__ = ("_summarizer", "_cache", "_policy", "_clock")

    def __init__(
        self,
        *,
        summarizer: Summarizer,
        policy: CompactPolicy,
        cache: CompactCache | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._summarizer = summarizer
        self._policy = policy
        self._cache = cache or CompactCache(enabled=policy.cache)
        self._clock = clock

    @property
    def policy(self) -> CompactPolicy:
        return self._policy

    @property
    def uses_llm(self) -> bool:
        return self._summarizer.available

    async def compact(
        self,
        messages: Sequence[ContextMessage],
        *,
        target_tokens: int,
        keep_recent: int | None = None,
        unit: Literal["turn", "step"] = "turn",
        segment: Segment = "window",
        reason: str = "",
    ) -> CompactResult:
        """把消息压到 ``target_tokens`` 以内。

        Args:
            target_tokens: 这一段能占的预算。**由预算层算出来**，不是自己猜的。
            keep_recent: 尾部**不动**的单位数（轮或步，取决于 ``unit``）；``None`` 用策略默认值。
            unit: 切分单位。``turn`` 用于**跨轮**的对话历史；``step`` 用于
                **一次 agent 运行内部**的消息（见 :func:`boundaries.by_step`）。
                运行内全部消息属于同一轮，按轮切**一步都压不掉**。
        """
        policy = self._policy
        messages = tuple(messages)
        if not messages:
            return CompactResult(blocks=(), note="没有历史")

        # **每次调用一个账本**（见类 docstring）：跨请求累积会让"这次压了几次"读错。
        ledger = Ledger(self._clock)

        if not policy.enabled:
            return CompactResult(
                blocks=blocks_from_messages(messages, segment=segment, level="L0"),
                note="压缩已关闭（context.linear.compact.enabled=false）",
            )

        keep = policy.keep_recent_turns if keep_recent is None else keep_recent
        unit_label = "步" if unit == "step" else "轮"
        groups = by_step(messages) if unit == "step" else by_turn(messages)
        if len(groups) <= keep:
            # 尾部保护已经覆盖了全部 —— **一个单位都不压**
            return CompactResult(
                blocks=blocks_from_messages(messages, segment=segment, level="L0"),
                note=f"历史只有 {len(groups)} {unit_label} ≤ 保护的 {keep} {unit_label}，不压缩",
            )

        head_turns, tail_turns = split_tail(groups, keep)
        head = flatten(head_turns)
        tail = flatten(tail_turns)
        tail_tokens = _tokens(tail)

        # ---- L1：零成本，无条件先做 -------------------------------------
        head_l1 = denoise(head, max_tool_chars=policy.l1_max_tool_output_chars)
        l1_tokens = _tokens(head_l1)
        ledger.record(
            level="L1",
            segment=segment,
            message_ids=[m.message_id for m in head if m.message_id],
            before_tokens=_tokens(head),
            after_tokens=l1_tokens,
            reason=reason or "预算不足，先做零成本去噪",
        )
        if l1_tokens + tail_tokens <= target_tokens or not self._summarizer.available:
            return CompactResult(
                blocks=(
                    *blocks_from_messages(head_l1, segment=segment, level="L1"),
                    *blocks_from_messages(tail, segment=segment, level="L0"),
                ),
                entries=ledger.entries,
                level_used="L1",
                insufficient=l1_tokens + tail_tokens > target_tokens,
                note=(
                    "L1 已够"
                    if l1_tokens + tail_tokens <= target_tokens
                    else "L1 不够，但摘要器不可用（降级）"
                ),
            )

        # ---- L2：一次摘要 -------------------------------------------------
        llm_calls = 0
        cached_hits = 0
        current_level: CompressionLevel = "L1"
        blocks: list[ContextBlock] = []
        l2_block: ContextBlock | None = None

        remaining_calls = policy.max_llm_calls
        if policy.allows("L2") and remaining_calls > 0:
            unit = CompactUnit(head_l1, fingerprint=_fingerprint(head_l1))
            l2_block, cached, called = await self._summarize(
                unit, level="L2", segment=segment, reason=reason, ledger=ledger
            )
            llm_calls += 1 if called else 0
            cached_hits += 1 if cached else 0
            remaining_calls -= 1 if called else 0

        if l2_block is not None:
            current_level = "L2"
            if _tokens_block(l2_block) + tail_tokens <= target_tokens:
                return CompactResult(
                    blocks=(l2_block, *blocks_from_messages(tail, segment=segment, level="L0")),
                    entries=ledger.entries,
                    level_used="L2",
                    llm_calls=llm_calls,
                    cached_hits=cached_hits,
                )

        # ---- L3：摘要再压成要点 -------------------------------------------
        if l2_block is not None and policy.allows("L3") and remaining_calls > 0:
            digest_unit = CompactUnit(
                (ContextMessage(role="user", content=l2_block.content, message_id=""),),
                fingerprint=fingerprint([_as_message(l2_block)]),
            )
            l3_block, cached, called = await self._summarize(
                digest_unit, level="L3", segment=segment,
                reason="摘要链自身超配额", ledger=ledger,
            )
            llm_calls += 1 if called else 0
            cached_hits += 1 if cached else 0
            if l3_block is not None:
                current_level = "L3"
                l2_block = l3_block

        if l2_block is not None:
            blocks = [l2_block, *blocks_from_messages(tail, segment=segment, level="L0")]
            total = _tokens_block(l2_block) + tail_tokens
            return CompactResult(
                blocks=tuple(blocks),
                entries=ledger.entries,
                level_used=current_level,
                llm_calls=llm_calls,
                cached_hits=cached_hits,
                insufficient=total > target_tokens,
                note=(
                    f"已压到 {current_level} 仍超出预算 {target_tokens}（{total}）"
                    if total > target_tokens
                    else ""
                ),
            )

        # ---- 摘要不可用：降级为 L1 并**如实报告** --------------------------
        _log.warning("摘要器不可用或返回空，上下文压缩降级为 L1（本次可能超预算）")
        return CompactResult(
            blocks=(
                *blocks_from_messages(head_l1, segment=segment, level="L1"),
                *blocks_from_messages(tail, segment=segment, level="L0"),
            ),
            entries=ledger.entries,
            level_used="L1",
            insufficient=True,
            note="摘要器不可用，降级为 L1；本次可能超出预算",
        )

    async def _summarize(
        self,
        unit: CompactUnit,
        *,
        level: CompressionLevel,
        segment: Segment,
        reason: str,
        ledger: Ledger,
    ) -> tuple[ContextBlock | None, bool, bool]:
        """走缓存 → 调摘要器 → 记账 → 写缓存。

        Returns:
            ``(块, 是否命中缓存, 是否真的调了模型)``
        """
        key = CompactCache.key(unit.fingerprint, level, self._summarizer.prompt_version)
        cached = self._cache.get(key)
        if cached is not None:
            return cached, True, False

        blocks = await self._summarizer.summarize(
            unit, level=level, segment=segment, reason=reason
        )
        if blocks is None:
            return None, False, True   # 调了但失败 —— 仍算一次调用（要计入上界）

        self._cache.put(key, blocks)
        ledger.record(
            level=level,
            segment=segment,
            message_ids=unit.message_ids,
            before_tokens=unit.tokens,
            after_tokens=_tokens_block(blocks),
            reason=reason,
            cache_key=key,
        )
        return blocks, False, True

    def clear_cache(self) -> None:
        self._cache.clear()

    async def digest(
        self,
        blocks: Sequence[ContextBlock],
        *,
        target_tokens: int,
        segment: Segment = "session_summary",
        reason: str = "",
    ) -> CompactResult:
        """对**已经压缩过**的块再做一次 **L3**（摘要链自压缩）。

        **这是两条策略汇合的地方**（`MC-15`）：策略二的「会话摘要链」自己会变长，
        对它做的就是多级压缩的 L3。所以它不该、也没有在 ``tiered.py`` 里重写一遍。
        """
        blocks = tuple(blocks)
        if not blocks:
            return CompactResult(blocks=(), note="没有摘要可压")

        ledger = Ledger(self._clock)
        total = sum(_tokens_block(block) for block in blocks)
        if total <= target_tokens:
            return CompactResult(blocks=blocks, level_used="L2", note="摘要链未超配额")

        if not self._policy.allows("L3") or not self._summarizer.available:
            return CompactResult(
                blocks=blocks,
                level_used="L2",
                insufficient=True,
                note=f"摘要链 {total} token 超过配额 {target_tokens}，但 L3 不可用（max_level 或摘要器）",
            )

        unit = CompactUnit(
            tuple(ContextMessage(role=block.role, content=block.content) for block in blocks),
            fingerprint=fingerprint([_as_message(b) for b in blocks]),
        )
        digest_block, cached, called = await self._summarize(
            unit, level="L3", segment=segment,
            reason=reason or "摘要链自身超配额", ledger=ledger,
        )
        if digest_block is None:
            return CompactResult(
                blocks=blocks, level_used="L2", insufficient=True,
                note="L3 压缩失败，保留原摘要链（不丢内容）",
            )

        # **把被压掉的摘要的范围并到产出上** —— 否则回捞的钩子会在这一级断掉：
        # 拿到的是"要点"，却指不回任何原始消息。
        merged_ids = tuple(
            dict.fromkeys(mid for block in blocks for mid in block.provenance.message_ids)
        )
        return CompactResult(
            blocks=(
                summary_block(
                    digest_block.content,
                    segment=segment,
                    level="L3",
                    message_ids=merged_ids,
                    note=reason or "摘要链自身超配额",
                ),
            ),
            entries=ledger.entries,
            level_used="L3",
            llm_calls=1 if called else 0,
            cached_hits=1 if cached else 0,
        )


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _tokens(messages: Sequence[ContextMessage]) -> int:
    return sum(estimate_text_tokens(m.content) for m in messages)


def _tokens_block(block: ContextBlock) -> int:
    return estimate_text_tokens(block.content)


def _fingerprint(messages: Sequence[ContextMessage]) -> str:
    return fingerprint(messages)


def _as_message(block: ContextBlock) -> ContextMessage:
    return ContextMessage(role=block.role, content=block.content)

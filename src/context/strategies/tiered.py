"""策略二：**短期 / 会话 / 长期** 三层（`FR-C-07..11`）。

```
├──── 短期：最近 N 轮**原文**（窗口） ────┤
├─ 过渡区：已出窗口、还没压 ─┤──── 会话记忆：每 K 轮压一次，累积成摘要链 ────┤
└────────────────── 长期：跨会话，来自 src/memory ──────────────────┘
```

**过渡区是被显式设计出来的一层，不是漏网之鱼**（`FR-C-09`）。
N 与 K 不同步时，总会有一段"出了窗口、还没到第 K 轮"的消息 ——
只写「窗口 + 摘要」的话，这段就是**静默消失**的，而且没有任何东西会报错。
所以这里给它一块自己的位置，并给它一个上限（超了**强制压缩**，不等第 K 轮）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from context.compact.boundaries import by_turn, flatten
from context.compact.levels import Compactor
from context.compact.types import blocks_from_messages
from context.context import ContextRequest
from context.resolver import MemoryResolver
from context.strategy import Collected
from context.types import ContextBlock, LedgerEntry, StrategyName

__all__ = [
    "InMemorySessionStore",
    "SessionMemory",
    "SessionMemoryStore",
    "TieredPolicy",
    "TieredStrategy",
]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TieredPolicy:
    """三层策略的参数（``context.tiered`` 段）。"""

    #: N：最近几轮**原文**（轮，不是 token —— 轮是用户能理解的单位）
    window_size: int = 10
    #: K：每几轮压缩一次
    compress_every: int = 6
    #: 过渡区上限。**必须有** —— 否则 K > N 时它会无限增长直到撑爆预算
    transition_max_rounds: int = 20
    dedupe: bool = True
    long_term_enabled: bool = True
    long_term_top_k: int = 8
    long_term_kinds: tuple[str, ...] = ("fact", "preference", "task_state", "lesson")

    @classmethod
    def from_config(cls, cfg: Mapping[str, object] | None) -> "TieredPolicy":
        data = dict(cfg or {})
        long_term = data.get("long_term")
        long_term = long_term if isinstance(long_term, Mapping) else {}
        policy = cls(
            window_size=int(data.get("window_size", 10)),
            compress_every=int(data.get("compress_every", 6)),
            transition_max_rounds=int(data.get("transition_max_rounds", 20)),
            dedupe=bool(data.get("dedupe", True)),
            long_term_enabled=bool(long_term.get("enabled", True)),
            long_term_top_k=int(long_term.get("top_k", 8)),
            long_term_kinds=tuple(str(x) for x in (long_term.get("kinds") or ())) or cls.long_term_kinds,
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.window_size <= 0:
            raise ValueError("context.tiered.window_size 必须为正")
        if self.compress_every <= 0:
            raise ValueError("context.tiered.compress_every 必须为正")
        if self.transition_max_rounds < 0:
            raise ValueError("context.tiered.transition_max_rounds 不能为负")
        if self.compress_every > self.window_size:
            # `C-C-5`：合法但会产生过渡区 —— **出声**，让人知道自己选了这条路径。
            _log.warning(
                "context.tiered 的 compress_every(%d) > window_size(%d)："
                "消息会在被压缩之前先出窗口，产生过渡区。这是合法配置（过渡区有上限兜底），"
                "但如果你希望它为空，把 compress_every 调到 ≤ window_size。",
                self.compress_every, self.window_size,
            )


@dataclass(frozen=True)
class SessionMemory:
    """一次会话的摘要链状态。

    ``summarized_turns`` 是**游标**：已经压到第几轮。没有它就无法区分
    "过渡区里哪些还没压"与"哪些已经进摘要了" —— 而那正是重复压缩的根源。
    """

    summaries: tuple[ContextBlock, ...] = ()
    summarized_turns: int = 0


class SessionMemoryStore(Protocol):
    """摘要链的存放处。

    ⚠ **目前是进程内实现**（`CQ-4` 未定）：落库会让跨进程/重启后仍可用，
    但那需要一张新表。存内存的代价是进程重启后摘要链清空 ——
    表现为"重启后模型忘了更早的上下文"，而原文还在 repo 里，可以重新压。
    """

    def get(self, session_id: str) -> SessionMemory: ...
    def put(self, session_id: str, memory: SessionMemory) -> None: ...


class InMemorySessionStore:
    """默认实现。**只活在进程里。**"""

    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: dict[str, SessionMemory] = {}

    def get(self, session_id: str) -> SessionMemory:
        return self._items.get(session_id, SessionMemory())

    def put(self, session_id: str, memory: SessionMemory) -> None:
        self._items[session_id] = memory

    def clear(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._items.clear()
        else:
            self._items.pop(session_id, None)


class TieredStrategy:
    """三层策略。"""

    name: StrategyName = "tiered"

    __slots__ = ("_compactor", "_resolver", "_policy", "_store")

    def __init__(
        self,
        *,
        compactor: Compactor,
        policy: TieredPolicy | None = None,
        resolver: MemoryResolver | None = None,
        store: SessionMemoryStore | None = None,
    ) -> None:
        self._compactor = compactor
        self._policy = policy or TieredPolicy()
        self._resolver = resolver
        self._store = store or InMemorySessionStore()

    @property
    def policy(self) -> TieredPolicy:
        return self._policy

    async def collect(
        self, request: ContextRequest, *, target_window_tokens: int
    ) -> Collected:
        policy = self._policy
        notes: list[str] = []
        degraded = ""

        turns = by_turn(request.history)
        window_start = max(0, len(turns) - policy.window_size)
        window_turns = turns[window_start:]

        memory = self._store.get(request.session_id)
        # 防御：会话历史变短（编辑/回滚）时游标可能越界 —— 夹住它，
        # 而不是让它把过渡区算成负数。
        summarized_until = min(memory.summarized_turns, window_start)
        transition_turns = turns[summarized_until:window_start]

        summaries = memory.summaries
        transition_blocks: tuple[ContextBlock, ...] = ()
        entries: list[LedgerEntry] = []

        if transition_turns:
            should_compress = (
                len(transition_turns) >= policy.compress_every
                or len(transition_turns) > policy.transition_max_rounds
            )
            if should_compress:
                reason = (
                    f"过渡区 {len(transition_turns)} 轮达到压缩条件"
                    f"（每 {policy.compress_every} 轮 / 上限 {policy.transition_max_rounds} 轮）"
                )
                # 过渡区**整体可压**（keep_recent_turns=0）：它的每一条都已经出了窗口
                result = await self._compactor.compact(
                    flatten(transition_turns),
                    target_tokens=max(64, target_window_tokens // 2),
                    keep_recent=0,
                    segment="transition",
                    reason=reason,
                )
                summaries = (*summaries, *result.blocks)
                entries.extend(result.entries)
                summarized_until = window_start
                self._store.put(
                    request.session_id,
                    SessionMemory(summaries=summaries, summarized_turns=summarized_until),
                )
                notes.append(f"过渡区已压缩并入摘要链：{len(transition_turns)} 轮")
                if result.insufficient and result.note:
                    degraded = result.note
            else:
                # **还没到压缩条件 → 原样保留**（`FR-C-09`）。
                # 直接丢掉是静默丢信息，正是本模块最不能犯的错。
                transition_blocks = blocks_from_messages(
                    flatten(transition_turns), segment="transition", level="L0"
                )
                notes.append(f"过渡区原样保留：{len(transition_turns)} 轮（未到压缩条件）")

        # 摘要链自身超配额 → 再压（L3）。这里正是"两条策略共用压缩零件"的兑现点。
        summary_blocks, digest_note, digest_degraded, digest_entries = await self._digest(
            summaries, target_tokens=max(64, target_window_tokens // 2)
        )
        entries.extend(digest_entries)
        if digest_note:
            notes.append(digest_note)
        degraded = degraded or digest_degraded

        blocks: dict[str, tuple[ContextBlock, ...]] = {
            "window": blocks_from_messages(flatten(window_turns), segment="window", level="L0"),
            "transition": transition_blocks,
            "session_summary": summary_blocks,
        }
        if self._resolver is not None and self._resolver.available and policy.long_term_enabled:
            recalled = await self._resolver.recall(request)
            if recalled:
                blocks["long_term"] = recalled

        return Collected(
            blocks=blocks, ledger=tuple(entries), degraded=degraded, notes=tuple(notes)
        )

    async def _digest(
        self, summaries: Sequence[ContextBlock], *, target_tokens: int
    ) -> tuple[tuple[ContextBlock, ...], str, str, tuple[LedgerEntry, ...]]:
        summaries = tuple(summaries)
        if not summaries:
            return (), "", "", ()
        result = await self._compactor.digest(
            summaries, target_tokens=target_tokens, segment="session_summary",
            reason="摘要链自身超配额",
        )
        return (
            result.blocks,
            result.note,
            result.note if result.insufficient else "",
            result.entries,
        )

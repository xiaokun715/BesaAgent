"""``ContextManager`` —— 上下文的**唯一入口**（`MC-14`）。

上层（``src/agent`` / ``src/multiagent``）只认这里的方法，
不知道现在跑的是哪条策略、压了几级、记忆有没有接上。

**编排顺序是有约束的**（`FR-C-12`）：

```
① 问 gateway 要 alias 的窗口
② 先用「原样的历史」估一版临时的预算 → 得到"历史那一段能占多少"
③ 把那个数丢给策略去压（策略据此决定压到多小）
④ 用**压完之后**的真实块重算预算（第一次是估的，第二次才是真的）
⑤ 按优先级裁剪 → 组装 → 合法化
```

**为什么要两遍**：第一遍必须在压缩**之前**，因为策略需要知道"目标是多小"；
第二遍必须在压缩**之后**，因为压缩真的改变了大小。
只做第一遍，配额是照着未压缩的体量分的；只做第二遍，策略不知道该压到多小。

**兜底降级**（`NFR-C-07`）：``build`` **不得**因为压缩或记忆失败而整体失败 ——
退回原样并告警，把 ``insufficient`` 标出来，让上层能看见。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from foundation.clock import Clock, SystemClock

from context.budget import BudgetPolicy, estimate_tokens
from context.builder import build_messages
from context.context import BuiltContext, ContextRequest
from context.resolver import MemoryResolver
from context.selector import ASSEMBLY_ORDER, dedupe, fit
from context.strategy import ContextStrategy, resolve_strategy
from context.types import (
    ContextBlock,
    ContextMessage,
    Provenance,
    Segment,
    StrategyName,
)

__all__ = [
    "DEFAULT_EVENTS",
    "ContextManager",
    "ContextPolicy",
    "EventSink",
    "NullEventSink",
]

_log = logging.getLogger(__name__)

#: 回捞原文的入口（由组合根注入，通常落在 ``repo`` 的 message 查询上）。
Rehydrator = Callable[[Sequence[str]], Awaitable[Sequence[ContextMessage]]]


class EventSink(Protocol):
    """事件出口。与 gateway 的 ``EventEmitter`` 同形状（``emit(name, payload)``）。"""

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...


class NullEventSink:
    """默认出口。**不是静默，而是让"没接事件"是一个明示的选择。**"""

    __slots__ = ()

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:  # noqa: D102
        return None


#: 上下文事件（架构概要设计 §8）。``<模块>.<对象>.<动作>``。
DEFAULT_EVENTS = {
    "built": "context.built",
    "compacted": "context.compacted",
    "compact_cached": "context.compact_cached",
    "compact_degraded": "context.compact_degraded",
    "dedupe_dropped": "context.dedupe_dropped",
    "segment_dropped": "context.segment_dropped",
    "long_term_injected": "context.long_term_injected",
    "settled": "context.settled",
}

#: 默认 system 提示。**不能是空串** —— 空 system 在某些厂商上等于"没有指令"。
_DEFAULT_SYSTEM = "你是一个软件测试助手。"


@dataclass(frozen=True)
class ContextPolicy:
    """``context`` 段的散项。"""

    strategy: StrategyName = "linear"
    dedupe: bool = True
    settle_on_close: bool = True
    #: 反查上下文窗口用的逻辑名（`DC-1`：窗口是模型属性，问 gateway 要）
    alias: str = "runtime.default"
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "ContextPolicy":
        data = dict(cfg or {})
        strategy = str(data.get("strategy", "linear"))
        if strategy not in ("linear", "tiered"):
            raise ValueError(
                f"context.strategy 只能是 linear | tiered，得到 {strategy!r}"
            )
        tiered = data.get("tiered") if isinstance(data.get("tiered"), Mapping) else {}
        dedupe = data.get("dedupe", tiered.get("dedupe", True))
        return cls(
            strategy=strategy,  # type: ignore[arg-type]
            dedupe=bool(dedupe),
            settle_on_close=bool(data.get("settle_on_close", True)),
            alias=str(data.get("alias") or "runtime.default"),
            budget=BudgetPolicy.from_config(data.get("budget")),
        )


class ContextManager:
    """上下文管理器。**由组合根装配。**"""

    def __init__(
        self,
        *,
        strategies: Mapping[StrategyName, ContextStrategy],
        policy: ContextPolicy | None = None,
        gateway: Any | None = None,
        resolver: MemoryResolver | None = None,
        rehydrator: Rehydrator | None = None,
        compactor: Any | None = None,
        clock: Clock | None = None,
        events: EventSink | None = None,
    ) -> None:
        self._strategies = dict(strategies)
        self._policy = policy or ContextPolicy()
        self._gateway = gateway
        self._resolver = resolver
        self._rehydrator = rehydrator
        #: 运行内压缩要用它（`docs/agent` 的 `AQ-1`）。与策略用的是**同一个**
        #: 压缩器实例 —— 分开建会让缓存与账本各有一套，而"这次压了几次"就没法回答。
        self._compactor = compactor
        self._clock = clock or SystemClock()
        self._events = events or NullEventSink()

    @property
    def policy(self) -> ContextPolicy:
        return self._policy

    @property
    def strategies(self) -> Mapping[StrategyName, ContextStrategy]:
        return dict(self._strategies)

    # ---------------------------------------------------------------- 构建
    async def build(self, request: ContextRequest) -> BuiltContext:
        """产出一个预算内的、合法的消息序列。"""
        strategy_name: StrategyName = request.strategy or self._policy.strategy
        strategy = resolve_strategy(strategy_name, self._strategies)

        system = request.system.strip() or _DEFAULT_SYSTEM
        model_window = self._resolve_window(self._policy.alias)

        fixed = {"system": estimate_tokens(system), "current": estimate_tokens(request.current_input)}

        # ② 临时预算：历史按"原样"估
        raw_window = sum(estimate_tokens(m.content) for m in request.history)
        provisional = self._policy.budget.plan(
            model_window=model_window, demands={**fixed, "window": raw_window}
        )

        # ③ 策略按这个目标去压 / 去召回
        collected = await strategy.collect(
            request, target_window_tokens=provisional.allocated.get("window", 0)
        )

        # ④ 用压完之后的真实块重算
        demands = dict(fixed)
        for segment, blocks in collected.blocks.items():
            demands[segment] = sum(estimate_tokens(b.content) for b in blocks)
        plan = self._policy.budget.plan(model_window=model_window, demands=demands)

        # 去重（`FR-C-11`）—— 只比对这批要注入的，有界
        deduped_blocks, deduped_notes = dedupe(collected.blocks, enabled=self._policy.dedupe)

        # ⑤ 裁剪 → 组装
        kept, fit_notes = fit(deduped_blocks, plan)
        ordered = _order(kept)

        degraded = collected.degraded
        if plan.insufficient and not degraded:
            degraded = "预算不足：连 system 与当前输入都放不下"

        try:
            messages = build_messages(
                system=system, blocks=ordered, current_input=request.current_input
            )
        except ValueError as exc:
            # 组装失败是**编程错误**，但上下文层不该把整轮对话打挂 ——
            # 退到最小可用形态（system + 当前输入）并出声。
            _log.warning("上下文组装失败，退到最小形态：%s", exc)
            messages = build_messages(
                system=system, blocks=(), current_input=request.current_input
            )
            degraded = f"组装降级：{exc}"

        built = BuiltContext(
            messages=messages,
            budget=plan,
            ledger=collected.ledger,
            deduped=deduped_notes,
            strategy=strategy_name,
            degraded_reason=degraded,
            blocks=ordered,
        )
        self._emit_build(built, collected.notes + fit_notes, request)
        return built

    async def compact_transcript(
        self,
        messages: "Sequence[Any]",
        *,
        keep_recent: int | None = None,
        unit: str = "step",
        ratio: float = 0.6,
        reason: str = "",
    ) -> Any:
        """**运行内压缩**（`docs/agent` `FR-A-08`）：把一次运行内部的消息压小。

        与 :meth:`build` 的区别是**切分单位**：`build` 处理的是跨轮的对话历史（按「轮」切），
        而一次 agent 运行内部的消息**全部属于同一轮** —— 按轮切一步都压不掉，
        所以要按「步」切（见 :func:`context.compact.by_step`）。

        Args:
            messages: ``gateway.messages.Message`` 序列（执行循环里的那些）。
            keep_recent: 最近几步**不参与压缩**（当前步永远要完整可见）。
            unit: 切分单位，默认 ``step``。
            ratio: 压到「模型可用输入」的这个比例以内。

        Returns:
            :class:`~context.compact.CompactResult`；调用方用 ``rebuild`` 还原成消息。
            压缩器缺席时返回一个「原样」的结果 —— **不是错误**（压缩是优化）。
        """
        from context.builder import from_message
        from context.compact.types import CompactResult, blocks_from_messages

        converted = tuple(from_message(message) for message in messages)
        if self._compactor is None:
            return CompactResult(
                blocks=blocks_from_messages(converted, segment="window", level="L0"),
                note="没有压缩器（运行内压缩未装配），消息原样保留",
            )

        target = max(256, int(self._resolve_window(self._policy.alias) * ratio))
        result = await self._compactor.compact(
            converted,
            target_tokens=target,
            keep_recent=keep_recent,
            unit=unit,  # type: ignore[arg-type]
            segment="window",
            reason=reason or "运行内压缩（按步）",
        )
        for entry in result.entries:
            self._emit(
                "compacted",
                {
                    "level": entry.level,
                    "segment": entry.segment,
                    "before_tokens": entry.before_tokens,
                    "after_tokens": entry.after_tokens,
                    "range": list(entry.message_ids),
                    "reason": entry.reason,
                },
            )
        return result

    async def settle(self, request: ContextRequest) -> bool:
        """会话结束时的沉淀（`FR-C-15`）。**失败只告警，不阻断。**"""
        if not self._policy.settle_on_close or self._resolver is None:
            return False
        try:
            settled = await self._resolver.settle(request)
        except Exception as exc:  # noqa: BLE001 — 记忆是旁路
            _log.warning("会话沉淀失败（不影响会话结束）：%s", exc)
            settled = False
        self._emit("settled", {"session_id": request.session_id, "settled": settled})
        return settled

    async def rehydrate(self, provenance: Provenance) -> tuple[ContextMessage, ...]:
        """按摘要的范围标识**取回原文**（`FR-C-16`）。

        **不是自动的**：它是给排障、评审、以及"模型说它需要细节"时的手动通道。

        Raises:
            RuntimeError: 没接回捞入口，或原文已不在（**明确出声，不返回空** ——
                返回空会让"这一段没有原文"和"原文查不到"这两件事混在一起）。
        """
        if self._rehydrator is None:
            raise RuntimeError(
                "没有接上原文回捞入口（rehydrator）—— 回捞需要经 repo 查 message 表。"
                "请由组合根注入；或关闭 context.rehydrate.enabled。"
            )
        ids = tuple(provenance.message_ids)
        if not ids:
            raise RuntimeError(
                f"这一段（segment={provenance.segment} level={provenance.level}）"
                "没有携带原始消息标识，无法回捞 —— 它可能是 L1 之前的原文块。"
            )
        return tuple(await self._rehydrator(ids))

    # ---------------------------------------------------------------- 内部
    def _resolve_window(self, alias: str) -> int:
        from_gateway: int | None = None
        if self._gateway is not None:
            query = getattr(self._gateway, "context_window", None)
            if callable(query):
                try:
                    from_gateway = query(alias)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("查询 alias %r 的上下文窗口失败：%s", alias, exc)
        return self._policy.budget.resolve_window(alias, from_gateway=from_gateway)

    def _emit_build(
        self, built: BuiltContext, notes: Sequence[str], request: ContextRequest
    ) -> None:
        budget = built.budget
        self._emit(
            "built",
            {
                "session_id": request.session_id,
                "strategy": built.strategy,
                "model_window": budget.model_window if budget else 0,
                "used": dict(budget.used) if budget else {},
                "dropped": dict(budget.dropped) if budget else {},
                "insufficient": built.insufficient,
                "degraded": built.degraded_reason,
                "notes": list(notes),
            },
        )
        for entry in built.ledger:
            self._emit(
                "compacted",
                {
                    "level": entry.level,
                    "segment": entry.segment,
                    "before_tokens": entry.before_tokens,
                    "after_tokens": entry.after_tokens,
                    "range": list(entry.message_ids),
                    "reason": entry.reason,
                },
            )
        if built.deduped:
            self._emit("dedupe_dropped", {"count": len(built.deduped), "items": list(built.deduped)[:8]})
        if budget is not None and budget.dropped:
            self._emit("segment_dropped", {"dropped": dict(budget.dropped)})
        long_term = sum(1 for b in built.blocks if b.provenance.segment == "long_term")
        if long_term:
            self._emit("long_term_injected", {"count": long_term})
        if built.degraded_reason:
            self._emit("compact_degraded", {"reason": built.degraded_reason, "strategy": built.strategy})

    def _emit(self, key: str, payload: Mapping[str, Any]) -> None:
        name = DEFAULT_EVENTS.get(key, f"context.{key}")
        try:
            self._events.emit(name, payload)
        except Exception as exc:  # noqa: BLE001 — 事件出口不该影响主流程
            _log.debug("上下文事件发送失败：%s", exc)


def _order(blocks: Mapping[Segment, Sequence[ContextBlock]]) -> tuple[ContextBlock, ...]:
    """按 ``ASSEMBLY_ORDER`` 排好（历史从旧到新，记忆作为背景在最前）。"""
    ordered: list[ContextBlock] = []
    for segment in ASSEMBLY_ORDER:
        ordered.extend(blocks.get(segment, ()))
    return tuple(ordered)

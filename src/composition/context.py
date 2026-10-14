"""把上下文层装配起来，并把它接到 gateway 与 memory 上。

与 ``composition/tools.py`` / ``composition/memory.py`` 同构 —— 存在的理由是
**两条依赖方向在这里交叉**：

- ``src/context`` **不能** import ``provider``（`DC-3` 补上的契约），
  所以「一次补全」由这里适配（:class:`~composition.memory.GatewayChat` 把
  ``Gateway.chat`` 包成 ``ChatModel`` 协议）；
- ``src/context`` **不能** import ``apps/``，所以数据库与向量由 app 层注入；
- 上下文要问 gateway「这个 alias 的窗口多大」（`DC-1`）—— 那是 gateway 的私有知识。

**同一份 ``ChatModel`` 适配器同时喂给记忆与上下文**：两者的协议形状是刻意保持一致的，
所以这里直接复用 :class:`composition.memory.GatewayChat`，不写第二份。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from composition.memory import GatewayChat

from context.compressor import build_compactor
from context.manager import ContextManager, ContextPolicy
from context.resolver import MemoryResolver
from context.strategies import build_strategies

__all__ = ["ContextBundle", "build_context", "summary_alias"]

_log = logging.getLogger(__name__)

#: 长期记忆默认召回的种类 —— 正好是记忆的四种 ``kind``：
#: 经验事实 / 用户画像 / 任务状态 / 经验教训。
_DEFAULT_LONG_TERM_KINDS: tuple[str, ...] = ("fact", "preference", "task_state", "lesson")


@dataclass(frozen=True)
class ContextBundle:
    """装配好的上下文层。"""

    manager: ContextManager
    #: 每条策略用的摘要别名（供 doctor 展示）
    summary_alias: str


def build_context(
    *,
    context_cfg: Mapping[str, Any] | None,
    gateway: Any,
    memory: Any | None = None,
    events: Any | None = None,
    clock: Any | None = None,
    rehydrator: Any | None = None,
) -> ContextBundle | None:
    """按配置装配上下文层。``context.enabled=false`` 时返回 ``None``。

    Args:
        memory: ``MemoryService``（``memory.enabled=false`` 时为 ``None``）。
            **没有它照样工作** —— 只是少了长期记忆那一层（`DV-9` 的同一条纪律）。
        rehydrator: 原文回捞入口（经 repo 查 message 表）。
            **目前为 ``None``** —— ``repo.message`` 还没实现；
            没有它时 ``rehydrate`` 会**明确报错**而不是返回空（`FR-C-16`）。
    """
    cfg = dict(context_cfg or {})
    if not bool(cfg.get("enabled", True)):
        _log.info("上下文层未启用（context.enabled=false）")
        return None

    policy = ContextPolicy.from_config(cfg)
    compact_cfg = _mapping(_mapping(cfg.get("linear")).get("compact"))

    chat = GatewayChat(gateway, summary_alias(compact_cfg))
    compactor = build_compactor(chat=chat, cfg=compact_cfg, clock=clock)

    tiered_cfg = _mapping(cfg.get("tiered"))
    long_term = _mapping(tiered_cfg.get("long_term"))
    resolver = MemoryResolver(
        memory,
        top_k=int(long_term.get("top_k", 8)),
        kinds=tuple(str(x) for x in (long_term.get("kinds") or ())) or _DEFAULT_LONG_TERM_KINDS,
    )

    strategies = build_strategies(
        compactor=compactor,
        resolver=resolver,
        tiered_cfg=tiered_cfg,
        linear_keep_recent_turns=(
            int(compact_cfg["keep_recent_turns"]) if "keep_recent_turns" in compact_cfg else None
        ),
    )
    manager = ContextManager(
        strategies=strategies,
        policy=policy,
        gateway=gateway,
        resolver=resolver,
        rehydrator=rehydrator,
        # **同一个压缩器**：运行内压缩（agent 用）与跨轮压缩（策略用）
        # 共用它会话内的缓存与账本，否则「这次压了几次」没法回答。
        compactor=compactor,
        clock=clock,
        events=events,
    )

    _log.info(
        "上下文层装配完成：策略=%s，压缩上限=%s，摘要别名=%s，长期记忆=%s，回捞=%s",
        policy.strategy,
        compactor.policy.max_level,
        summary_alias(compact_cfg),
        "开" if resolver.available else "关",
        "开" if rehydrator is not None else "关（repo.message 未实现）",
    )
    return ContextBundle(manager=manager, summary_alias=summary_alias(compact_cfg))


def summary_alias(compact_cfg: Mapping[str, Any]) -> str:
    return str(compact_cfg.get("summary_alias") or "runtime.default")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

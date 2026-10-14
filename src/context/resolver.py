"""与**长期记忆**的接缝（`FR-C-10` / `FR-C-15`）。

本模块**不实现记忆** —— 它是 ``src/memory``。这里只做三件事：

1. **召回**：拿当前任务与输入去问记忆要相关的条目；
2. **转形**：把记忆条目变成上下文块（**保留它的不可信标记**）；
3. **沉淀**：会话结束时把这段对话递回记忆（走它的三道闸门，本模块不判定该不该记）。

**记忆不可用时整段缺席，而不是报错**（`NFR-C-07`）：长期记忆是**增益**，
没有它上下文照样组得出来 —— 只是少了跨会话的那一层。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from context.context import ContextRequest
from context.types import ContextBlock, ContextMessage, Provenance

__all__ = ["MemoryResolver"]

_log = logging.getLogger(__name__)


class MemoryResolver:
    """把 ``MemoryService`` 包成"上下文的一块来源"。"""

    __slots__ = ("_memory", "_top_k", "_kinds")

    def __init__(
        self,
        memory: Any | None = None,
        *,
        top_k: int = 8,
        kinds: Sequence[str] = ("fact", "preference", "task_state", "lesson"),
    ) -> None:
        self._memory = memory
        self._top_k = top_k
        self._kinds = tuple(kinds)

    @property
    def available(self) -> bool:
        """记忆服务在不在、开没开。

        ``None``（默认，``memory.enabled=false``）时**整段缺席** ——
        这不是残废版，而是一条完整的路径（与 ``DV-9`` 同源）。
        """
        if self._memory is None:
            return False
        return bool(getattr(self._memory, "enabled", True))

    async def recall(self, request: ContextRequest) -> tuple[ContextBlock, ...]:
        """召回长期记忆。**失败返回空元组，不抛。**"""
        if not self.available:
            return ()
        query = request.recall_query()
        if not query.strip():
            return ()

        try:
            hits = await self._memory.recall(
                query,
                ctx=_memory_context(request),
                kinds=list(self._kinds),
                top_k=self._top_k,
            )
        except Exception as exc:  # noqa: BLE001 — 记忆是旁路
            _log.warning("长期记忆召回失败（本次不注入记忆）：%s", exc)
            return ()

        blocks: list[ContextBlock] = []
        for hit in hits:
            item = getattr(hit, "item", None)
            if item is None:
                continue
            blocks.append(
                ContextBlock(
                    # **保留记忆自己的不可信标记**：记忆源于对话，可能被注入过。
                    # 在这里"洗白"它等于把注入源直接送进上下文（`NFR-M-02`）。
                    content=item.to_model_text(),
                    role="user",
                    provenance=Provenance(
                        segment="long_term",
                        confidence=getattr(hit, "score", None),
                        note=f"memory:{item.kind}",
                    ),
                )
            )
        return tuple(blocks)

    async def settle(self, request: ContextRequest) -> bool:
        """会话结束时把这段对话递回记忆（`FR-C-15`）。**失败只告警。**"""
        if not self.available or not request.history:
            return False
        turns = [
            _memory_turn(message) for message in request.history if message.content.strip()
        ]
        if not turns:
            return False
        try:
            await self._memory.remember(
                turns,
                source=_memory_source(request),
                ctx=_memory_context(request),
            )
        except Exception as exc:  # noqa: BLE001 — 沉淀不得阻断会话结束
            _log.warning("会话沉淀失败（记忆是旁路，不影响会话结束）：%s", exc)
            return False
        return True


# --------------------------------------------------------------------------- #
# 辅助：领域形态 → memory 的形态（**只需两三个字段**，所以是就地映射）
# --------------------------------------------------------------------------- #


def _memory_context(request: ContextRequest) -> Any:
    from memory.types import MemoryContext

    return MemoryContext(
        scope="session",
        session_id=request.session_id,
        caller=request.caller,
        trace_id=request.trace_id,
    )


def _memory_turn(message: ContextMessage) -> Any:
    from memory.types import MemoryTurn

    return MemoryTurn(role=message.role, content=message.content)


def _memory_source(request: ContextRequest) -> Any:
    from memory.types import MemorySource

    return MemorySource(
        kind="conversation",
        session_id=request.session_id,
        trace_id=request.trace_id,
        caller=request.caller,
    )

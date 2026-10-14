"""记忆层：跨会话的**可检索、会退场**的记忆（`src/memory`）。

**它解决的问题**：七阶段是长链路、跨会话的工作。一个会话里学到的偏好与教训，
如果只活在这一次的上下文里，下一个会话就从零开始 —— 而上下文窗口是稀缺资源。
记忆层把「可复用的结论」沉淀下来，按相关性取回，并让过时的自己退场。

**与本模块的两条纪律有关的两件事**（读代码前先记住）：

1. **写入之前判定**。记忆的错误会**复利**：被检索、被采用、被强化，越用越"真"。
   所以三道闸门（:mod:`memory.policy`）与四分支判定（:mod:`memory.resolver`）
   都发生在**落库之前** —— 写进去之后再清理，成本与污染都无法回退。
2. **只有「被采用」才强化**。单纯的「被检索到」不算数，否则一条错误记忆会因为
   「总被取出来」而自我升权（:func:`memory.types.saturating_weight`）。

**依赖方向（硬约束）**：本模块只依赖 ``foundation`` / ``gateway``（经注入的协议）/
``repo``。**不得** import ``provider``（契约 4 机械强制）；**不得** import
``agent`` / ``multiagent``（依赖方向应反向 —— 由它们调记忆）。

**对外只有一个面**：:class:`memory.manager.MemoryService`。
上层不该知道闸门有几道、检索怎么混、衰减什么曲线 ——
与 ``ToolExecutor`` 是工具层唯一入口同构。
"""

from __future__ import annotations

from memory.decay import DecayPolicy, utc_from_epoch
from memory.extractor import ExtractPolicy, Extractor, render_conversation
from memory.manager import DEFAULT_EVENTS, MemoryLimits, MemoryService, NullEventSink
from memory.policy import GatePolicy, WriteGate
from memory.reflect import ReflectPolicy, Reflector, find_clusters
from memory.resolver import ResolveDecision, Resolver
from memory.retriever import RetrievalPolicy, Retriever, lexical_overlap
from memory.scheduler import MaintenancePolicy, next_run_after
from memory.store import MemoryStore, content_hash, item_from_view, view_from_item
from memory.types import (
    MEMORY_KINDS,
    MEMORY_OPS,
    MEMORY_SCOPES,
    MEMORY_STATES,
    PROTECTED_KINDS,
    RESOLUTIONS,
    CandidateRun,
    ChatModel,
    Embedder,
    GateName,
    GateVerdict,
    MaintenanceReport,
    MemoryCandidate,
    MemoryContext,
    MemoryItem,
    MemoryKind,
    MemoryOp,
    MemoryScope,
    MemorySource,
    MemoryState,
    MemoryTurn,
    RememberOutcome,
    Resolution,
    RetrievalHit,
    is_stronger,
    saturating_weight,
)

__all__ = [
    "DEFAULT_EVENTS",
    "MEMORY_KINDS",
    "MEMORY_OPS",
    "MEMORY_SCOPES",
    "MEMORY_STATES",
    "PROTECTED_KINDS",
    "RESOLUTIONS",
    "CandidateRun",
    "ChatModel",
    "DecayPolicy",
    "Embedder",
    "ExtractPolicy",
    "Extractor",
    "GateName",
    "GatePolicy",
    "GateVerdict",
    "MaintenancePolicy",
    "MaintenanceReport",
    "MemoryCandidate",
    "MemoryContext",
    "MemoryItem",
    "MemoryKind",
    "MemoryLimits",
    "MemoryOp",
    "MemoryScope",
    "MemoryService",
    "MemorySource",
    "MemoryState",
    "MemoryStore",
    "MemoryTurn",
    "NullEventSink",
    "ReflectPolicy",
    "Reflector",
    "RememberOutcome",
    "ResolveDecision",
    "Resolution",
    "RetrievalHit",
    "RetrievalPolicy",
    "Retriever",
    "Resolver",
    "WriteGate",
    "content_hash",
    "find_clusters",
    "is_stronger",
    "item_from_view",
    "lexical_overlap",
    "next_run_after",
    "render_conversation",
    "saturating_weight",
    "utc_from_epoch",
    "view_from_item",
]

"""``MemoryService`` —— 记忆模块**唯一**的对外面（`MR-15`）。

上层（``src/agent`` / ``src/context``）只认这几个方法，
不知道闸门有几道、检索怎么混、衰减什么曲线 —— 与 ``ToolExecutor`` 是工具层
唯一入口同构。

**两条路线都收在这里**：

- :meth:`remember` / :meth:`recall` / :meth:`reinforce` —— **在线**（写时判定）；
- :meth:`maintain` —— **离线**（定时判定：衰减 + 蒸馏 + 反思）。

**旁路纪律**（`NFR-M-07`）：四个方法**都不得**把异常抛给调用方 ——
``remember`` / ``reinforce`` 失败只记事件告警；``recall`` 失败返回空。
记忆是旁路，不该拖垮会话。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

from foundation.clock import Clock, SystemClock
from foundation.ids import new_id
from memory.decay import DecayPolicy, utc_from_epoch
from memory.extractor import Extractor
from memory.policy import WriteGate
from memory.reflect import Reflector, find_clusters
from memory.resolver import Resolver
from memory.retriever import Retriever
from memory.scheduler import MaintenancePolicy
from memory.store import MemoryStore, content_hash
from memory.types import (
    Embedder,
    MaintenanceReport,
    MemoryCandidate,
    MemoryContext,
    MemoryItem,
    MemoryKind,
    MemorySource,
    MemoryTurn,
    RememberOutcome,
    RetrievalHit,
)

__all__ = [
    "DEFAULT_EVENTS",
    "MemoryLimits",
    "MemoryService",
    "NullEventSink",
]

_log = logging.getLogger(__name__)


class EventSink(Protocol):
    """事件出口。与 gateway 的 ``EventEmitter`` 同形状（``emit(name, payload)``）。"""

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...


class NullEventSink:
    """默认出口：什么都不做。**不是为了静默，而是为了让「没接事件」是个明示的选择。**"""

    __slots__ = ()

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:  # noqa: D102
        return None


#: 记忆事件名（架构文档 §8）。``<模块>.<对象>.<动作>``。
DEFAULT_EVENTS = {
    "rejected": "memory.candidate.rejected",
    "added": "memory.added",
    "merged": "memory.merged",
    "reinforced": "memory.reinforced",
    "updated": "memory.updated",
    "conflicted": "memory.conflicted",
    "decayed": "memory.decayed",
    "reflected": "memory.reflected",
    "degraded": "memory.degraded",
}


@dataclass(frozen=True)
class MemoryLimits:
    """总开关与运行期参数（``memory`` 段的散项）。"""

    enabled: bool = False
    #: 检索不可用时（`FR-M-15`）：``defer`` 挂起告警 / ``reject`` 拒绝。
    on_retrieval_unavailable: Literal["defer", "reject"] = "defer"
    #: ``task_state`` 的硬时效（`DM-10`）。``0`` = 不设。
    task_state_ttl_hours: float = 168.0
    #: 回灌前是否带不可信标记（`NFR-M-02`）。
    mark_untrusted: bool = True
    #: 写入路径检索时找几个「对手」。
    write_top_k: int = 10
    #: refletion 轨迹回溯窗口（小时）。
    reflect_window_hours: float = 24.0
    #: 单轮蒸馏最多处理几簇。
    max_clusters_per_run: int = 5

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "MemoryLimits":
        data = dict(cfg or {})
        kinds = _mapping(data.get("kinds"))
        write = _mapping(data.get("write"))
        write_retrieve = _mapping(write.get("retrieve"))
        retrieve = _mapping(data.get("retrieve"))

        mode = str(write.get("on_retrieval_unavailable", "defer"))
        if mode not in ("defer", "reject"):
            raise ValueError(
                f"memory.write.on_retrieval_unavailable 只能是 defer|reject，得到 {mode!r}"
            )
        return cls(
            enabled=bool(data.get("enabled", False)),
            on_retrieval_unavailable=mode,  # type: ignore[arg-type]
            task_state_ttl_hours=float(kinds.get("task_state_ttl_hours", 168.0)),
            mark_untrusted=bool(retrieve.get("mark_untrusted", True)),
            write_top_k=int(write_retrieve.get("top_k", 10)),
        )


class MemoryService:
    """记忆服务。**由组合根装配，注入 app 层建好的 Database 与 VectorStore。**"""

    def __init__(
        self,
        *,
        database: Any | None,
        store_factory: Callable[[Any], MemoryStore],
        gate: WriteGate,
        extractor: Extractor,
        retriever: Retriever,
        resolver: Resolver,
        reflector: Reflector,
        decay: DecayPolicy,
        scheduler: MaintenancePolicy,
        limits: MemoryLimits,
        embedder: Embedder | None = None,
        clock: Clock | None = None,
        events: EventSink | None = None,
    ) -> None:
        self._db = database
        self._store_factory = store_factory
        self._gate = gate
        self._extractor = extractor
        self._retriever = retriever
        self._resolver = resolver
        self._reflector = reflector
        self._decay = decay
        self._scheduler = scheduler
        self._limits = limits
        self._embedder = embedder
        self._clock = clock or SystemClock()
        self._events = events or NullEventSink()

    # ------------------------------------------------------------------ 属性
    @property
    def enabled(self) -> bool:
        return self._limits.enabled and self._db is not None

    @property
    def decay_policy(self) -> DecayPolicy:
        return self._decay

    @property
    def scheduler(self) -> MaintenancePolicy:
        return self._scheduler

    def render_for_model(self, hits: Sequence[RetrievalHit]) -> str:
        """把检索结果渲染成给模型的文本（`NFR-M-02`）。

        调用方（``context``）拿它拼进窗口。**带不可信标记**的开关在这里生效 ——
        「不可信」是记忆的固有属性，不该由每个调用点各自决定要不要标。
        """
        if not hits:
            return ""
        lines: list[str] = []
        for hit in hits:
            if self._limits.mark_untrusted:
                lines.append(hit.item.to_model_text())
            else:
                lines.append(hit.item.content)
        return "\n\n".join(lines)

    # ------------------------------------------------------------ 路线一：写
    async def remember(
        self,
        conversation: Sequence[MemoryTurn] | str,
        *,
        source: MemorySource,
        ctx: MemoryContext,
    ) -> RememberOutcome:
        """从一段对话里提取并写入记忆（路线一）。**失败降级，不抛。**"""
        if not self.enabled:
            return RememberOutcome(None, reason="记忆未启用或没有数据库，本次不写入")

        now = utc_from_epoch(self._clock.now())
        if self._db is None:  # pragma: no cover - enabled 已经挡过
            return RememberOutcome(None, reason="没有数据库")

        try:
            async with self._db.transaction() as tx:
                store = self._store_factory(tx)
                candidates = await self._extractor.extract(conversation, source=source, ctx=ctx)
                if not candidates:
                    return RememberOutcome(None, reason="没有提取到可记的内容")

                outcomes = [
                    await self._store_candidate(store, candidate, ctx=ctx, now=now)
                    for candidate in candidates
                ]
                return _aggregate(outcomes)
        except Exception as exc:  # noqa: BLE001 — 记忆是旁路
            _log.warning("记忆写入失败（旁路，不影响会话）：%s", exc)
            self._emit("degraded", {"phase": "remember", "error": str(exc)}, ctx)
            return RememberOutcome(None, reason=f"写入降级：{exc}")

    async def remember_candidate(
        self, candidate: MemoryCandidate, *, ctx: MemoryContext
    ) -> RememberOutcome:
        """写入**一个显式候选**（不过提取那一步）。供反思/蒸馏与外部调用。"""
        if not self.enabled or self._db is None:
            return RememberOutcome(None, reason="记忆未启用或没有数据库")
        now = utc_from_epoch(self._clock.now())
        try:
            async with self._db.transaction() as tx:
                store = self._store_factory(tx)
                return await self._store_candidate(store, candidate, ctx=ctx, now=now)
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆候选写入失败（旁路）：%s", exc)
            self._emit("degraded", {"phase": "remember_candidate", "error": str(exc)}, ctx)
            return RememberOutcome(None, reason=f"写入降级：{exc}")

    async def _store_candidate(
        self,
        store: MemoryStore,
        candidate: MemoryCandidate,
        *,
        ctx: MemoryContext,
        now: datetime,
    ) -> RememberOutcome:
        # ① 三道闸门
        verdict = await self._gate.judge(candidate, ctx)
        if not verdict.allow:
            await store.append_op(
                op_id=str(new_id()), op="add", before_hash="", after_hash="",
                reason=f"[{verdict.gate}] {verdict.reason}",
                actor=ctx.caller or "system", source_trace_id=ctx.trace_id,
                scope=ctx.scope, tenant_id=ctx.tenant_id,
                payload={"rejected": True, "gate": verdict.gate}, now=now,
            )
            self._emit(
                "rejected",
                {"gate": verdict.gate, "reason": verdict.reason, "kind": verdict.kind},
                ctx, subject=verdict.gate, outcome="rejected",
            )
            return RememberOutcome(None, rejected_gate=verdict.gate, reason=verdict.reason, candidates=1)

        content = verdict.content or candidate.content
        kind: MemoryKind = verdict.kind or candidate.kind_hint or "fact"
        confidence = verdict.confidence if verdict.confidence is not None else (candidate.confidence or 0.5)
        digest = content_hash(content, ctx.scope)

        # ② 幂等：同指纹已在 → 不新建（`FR-M-01`）
        existing = await store.find_by_hash(digest, ctx)
        if existing is not None:
            weight = self._decay.reinforced_weight(existing)
            await store.reinforce(existing.memory_id, weight=weight, used=False, now=now)
            await self._append(store, "reinforce", existing.memory_id, before=digest, after=digest,
                               reason="同内容指纹已存在，强化既有记忆（幂等）", ctx=ctx, now=now)
            self._emit("reinforced", {"reason": "idempotent"}, ctx, subject=existing.kind, outcome="reinforced")
            return RememberOutcome("reinforce", memory_id=existing.memory_id, reason="同指纹命中，已强化", candidates=1)

        # ③ 找对手（写入路径上的那次检索，不设上限 —— 截断会漏掉该比较的那条）
        try:
            hits = await self._retriever.retrieve(
                store, query=content, ctx=ctx, top_k=self._limits.write_top_k, now=now, bounded=False
            )
        except Exception as exc:  # noqa: BLE001
            return await self._on_retrieval_unavailable(store, candidate, ctx, exc, now)

        # ④ 四分支判定
        decision = await self._resolver.resolve(candidate, hits, now=now, confidence=confidence)

        if decision.resolution == "add":
            return await self._add(store, content, digest, kind, confidence, ctx, now)

        if decision.resolution == "reinforce" and decision.target is not None:
            weight = self._decay.reinforced_weight(decision.target)
            await store.reinforce(decision.target.memory_id, weight=weight, used=False, now=now)
            await self._append(store, "reinforce", decision.target.memory_id, before=digest,
                               after=digest, reason=decision.reason, ctx=ctx, now=now)
            self._emit("reinforced", {"reason": decision.reason}, ctx,
                       subject=decision.target.kind, outcome="reinforced")
            return RememberOutcome("reinforce", memory_id=decision.target.memory_id,
                                   reason=decision.reason, candidates=1)

        if decision.resolution == "merge" and decision.target is not None:
            extra = 1 + len(decision.merged)
            weight = self._decay.reinforced_weight(decision.target)
            await store.reinforce(decision.target.memory_id, weight=weight, used=False,
                                  evidence_inc=extra, now=now)
            await store.merge([hit.memory_id for hit in decision.merged], decision.target.memory_id, now=now)
            await self._append(store, "merge", decision.target.memory_id, before=digest,
                               after=digest, reason=decision.reason, ctx=ctx, now=now,
                               payload={"merged": [str(m.memory_id) for m in decision.merged]})
            self._emit("merged", {"merged": len(decision.merged)}, ctx,
                       subject=decision.target.kind, outcome="merged")
            return RememberOutcome("merge", memory_id=decision.target.memory_id,
                                   reason=decision.reason, candidates=1)

        if decision.resolution == "update" and decision.target is not None:
            return await self._update(store, decision, content, digest, kind, confidence, ctx, now)

        # conflict：**不推翻**既有事实，只记冲突（`MR-4`）
        await self._append(store, "update", decision.target.memory_id if decision.target else None,
                           before=(decision.target.content if decision.target else ""), after=content,
                           reason=decision.reason, ctx=ctx, now=now, payload={"conflict": True})
        self._emit("conflicted", {"reason": decision.reason}, ctx,
                   subject=(decision.target.kind if decision.target else ""), outcome="conflict")
        return RememberOutcome("conflict", memory_id=(decision.target.memory_id if decision.target else None),
                               reason=decision.reason, candidates=1)

    async def _add(
        self, store: MemoryStore, content: str, digest: str, kind: MemoryKind,
        confidence: float, ctx: MemoryContext, now: datetime,
    ) -> RememberOutcome:
        # 同内容曾被遗忘过 → **复活**，保留原 id 与轨迹（`MQ-6`）
        tombstone = await store.find_tombstone(digest, ctx)
        if tombstone is not None:
            await store.revive(tombstone.memory_id, weight=1.0, now=now)
            await self._append(store, "add", tombstone.memory_id, before=digest, after=digest,
                               reason="同内容曾被遗忘，复活并重新计时", ctx=ctx, now=now)
            self._emit("added", {"revived": True}, ctx, subject=tombstone.kind, outcome="added")
            return RememberOutcome("add", memory_id=tombstone.memory_id, reason="复活既有记忆", candidates=1)

        item = self._build_item(content, digest, kind, confidence, ctx, now)
        embedding = await self._embed(content)
        saved = await store.save(item, embedding=embedding)
        await self._append(store, "add", saved.memory_id, before="", after=digest,
                           reason="新增记忆", ctx=ctx, now=now)
        self._emit("added", {"kind": kind}, ctx, subject=kind, outcome="added")
        return RememberOutcome("add", memory_id=saved.memory_id, reason="新增", candidates=1)

    async def _update(
        self, store: MemoryStore, decision: Any, content: str, digest: str,
        kind: MemoryKind, confidence: float, ctx: MemoryContext, now: datetime,
    ) -> RememberOutcome:
        target: MemoryItem = decision.target
        item = self._build_item(content, digest, kind, confidence, ctx, now)
        item = replace(
            item,
            version=target.version + 1,
            supersedes_id=target.memory_id,
            importance=target.importance,
        )
        embedding = await self._embed(content)
        saved = await store.save(item, embedding=embedding)
        await store.supersede(target.memory_id, saved.memory_id, now=now)
        await self._append(store, "update", saved.memory_id, before=content_hash(target.content, target.scope),
                           after=digest, reason=decision.reason, ctx=ctx, now=now,
                           payload={"superseded": str(target.memory_id)})
        self._emit("updated", {"superseded": str(target.memory_id)}, ctx, subject=kind, outcome="updated")
        return RememberOutcome("update", memory_id=saved.memory_id, reason=decision.reason, candidates=1)

    async def _on_retrieval_unavailable(
        self, store: MemoryStore, candidate: MemoryCandidate, ctx: MemoryContext,
        exc: Exception, now: datetime,
    ) -> RememberOutcome:
        """检索不可用 —— **不得静默新建**（`FR-M-15`）。"""
        mode = self._limits.on_retrieval_unavailable
        await self._append(store, "add", None, before="", after="",
                           reason=f"检索不可用，按 {mode} 处理：{exc}", ctx=ctx, now=now,
                           payload={"deferred": mode == "defer"})
        self._emit("degraded", {"phase": "retrieve", "mode": mode, "error": str(exc)}, ctx,
                   subject="retrieve", outcome="degraded")
        return RememberOutcome(None, reason=f"检索不可用（{mode}）：{exc}", candidates=1)

    def _build_item(
        self, content: str, digest: str, kind: MemoryKind, confidence: float,
        ctx: MemoryContext, now: datetime,
    ) -> MemoryItem:
        valid_to = None
        if kind == "task_state" and self._limits.task_state_ttl_hours > 0:
            valid_to = now + timedelta(hours=self._limits.task_state_ttl_hours)
        return MemoryItem(
            memory_id=new_id(),
            kind=kind,
            content=content,
            scope=ctx.scope,
            tenant_id=ctx.tenant_id,
            owner_id=ctx.owner_id,
            confidence=confidence,
            importance=0.7 if kind == "preference" else 0.5,
            source=MemorySource(
                kind="agent", session_id=ctx.session_id, trace_id=ctx.trace_id, caller=ctx.caller
            ),
            valid_from=now,
            valid_to=valid_to,
            created_at=now,
            updated_at=now,
            metadata={"content_hash": digest},
        )

    async def _embed(self, content: str) -> tuple[float, ...] | None:
        """给自己的记忆向量化。**失败返回 ``None``** —— 只落元数据，
        退到结构化 + 关键词检索（`FR-M-17` 的降级路径，不是错误）。"""
        if self._embedder is None:
            return None
        try:
            vectors = await self._embedder.embed([content])
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆向量化失败，本条只落元数据（退到结构化检索）：%s", exc)
            return None
        return tuple(float(x) for x in vectors[0]) if vectors else None

    # ------------------------------------------------------------ 检索
    async def recall(
        self,
        query: str,
        *,
        ctx: MemoryContext,
        kinds: Sequence[str] | None = None,
        top_k: int | None = None,
    ) -> tuple[RetrievalHit, ...]:
        """取回与 ``query`` 相关的记忆。**失败返回空元组，不抛**（`NFR-M-07`）。"""
        if not self.enabled or self._db is None:
            return ()
        now = utc_from_epoch(self._clock.now())
        try:
            async with self._db.transaction() as tx:
                store = self._store_factory(tx)
                hits = await self._retriever.retrieve(
                    store, query=query, ctx=ctx, kinds=kinds, top_k=top_k, now=now
                )
                for hit in hits:
                    # 只记「被取出来过」。**不动权重** —— 强化要看是否被采用（`FR-M-08`）。
                    await store.mark_accessed(hit.item.memory_id, now=now)
                return hits
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆检索失败（旁路，返回空）：%s", exc)
            self._emit("degraded", {"phase": "recall", "error": str(exc)}, ctx)
            return ()

    async def reinforce(
        self, memory_id: UUID, *, used: bool, ctx: MemoryContext
    ) -> None:
        """反哺：**只有被采用时才强化**（`FR-M-08`）。失败降级，不抛。"""
        if not self.enabled or self._db is None:
            return
        now = utc_from_epoch(self._clock.now())
        try:
            async with self._db.transaction() as tx:
                store = self._store_factory(tx)
                item = await store.find_by_id(memory_id)
                if item is None:
                    return
                if not used:
                    await store.mark_accessed(memory_id, now=now)
                    return
                weight = self._decay.reinforced_weight(item)
                await store.reinforce(memory_id, weight=weight, used=True, now=now)
                await self._append(store, "reinforce", memory_id, before="", after="",
                                   reason="检索命中且被采用，反哺强化", ctx=ctx, now=now)
                self._emit("reinforced", {"used": True}, ctx, subject=item.kind, outcome="reinforced")
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆反哺失败（旁路）：%s", exc)

    # ------------------------------------------------------------ 路线二：维护
    async def maintain(self, *, now: datetime | None = None) -> MaintenanceReport:
        """一次离线维护：衰减 → 蒸馏 → 反思（`FR-M-18..20`）。**失败降级，不抛。**"""
        if not self.enabled or self._db is None:
            return MaintenanceReport(reason="记忆未启用或没有数据库")
        moment = now or utc_from_epoch(self._clock.now())
        try:
            async with self._db.transaction() as tx:
                store = self._store_factory(tx)
                last = await store.last_maintenance_at()
                if not self._scheduler.due(last, moment):
                    return MaintenanceReport(reason="未到维护时机")

                report = await self._sweep_decay(store, moment)
                report = report.merge(await self._distill(store, moment))
                report = report.merge(await self._reflect(store, moment, last))

                # 维护汇总行 —— 它同时是**下一次维护的游标**（`FR-M-18`）。
                await self._append(
                    store, "decay", None, before="", after="",
                    reason="离线维护完成", ctx=MemoryContext(trace_id="maintenance"), now=moment,
                    payload={
                        "scanned": report.scanned, "tombstoned": report.tombstoned,
                        "distilled": report.distilled, "reflected": report.reflected,
                    },
                )
                self._emit("decayed", {"tombstoned": report.tombstoned}, MemoryContext(), outcome="decayed")
                return report
        except Exception as exc:  # noqa: BLE001
            _log.warning("记忆离线维护失败（旁路）：%s", exc)
            return MaintenanceReport(reason=f"维护降级：{exc}")

    async def _sweep_decay(self, store: MemoryStore, now: datetime) -> MaintenanceReport:
        policy = self._scheduler
        older_than = policy.older_than(now)
        scanned = tombstoned = decayed = 0

        for _ in range(policy.max_batches_per_run):
            batch = await store.sweep_candidates(
                limit=policy.batch_size, older_than=older_than, now=now
            )
            if not batch:
                break
            for item in batch:
                scanned += 1
                if self._decay.effective_weight(item, now) < item.weight:
                    decayed += 1
                if self._decay.next_state(item, now) != "active":
                    await store.tombstone(item.memory_id, now=now)
                    await self._append(store, "decay", item.memory_id, before=item.content[:64],
                                       after="", reason="权重衰减至阈值以下，转遗忘",
                                       ctx=MemoryContext(scope=item.scope, tenant_id=item.tenant_id),
                                       now=now)
                    tombstoned += 1
            if len(batch) < policy.batch_size:
                break

        return MaintenanceReport(scanned=scanned, decayed=decayed, tombstoned=tombstoned)

    async def _distill(self, store: MemoryStore, now: datetime) -> MaintenanceReport:
        policy = self._reflector.policy
        if not policy.enabled:
            return MaintenanceReport()
        limit = self._scheduler.batch_size
        distilled = rejected = 0

        # 蒸馏候选固定取「事实」与「经验」两类 —— 偏好与任务态冗余度低，且更敏感。
        members = await store.cluster_members(kinds=["fact", "lesson"], limit=limit)
        # **必须先按 (租户, 作用域) 分组再聚类**：否则连通分量会把不同作用域的记忆
        # 连成一簇，而蒸馏出来的那条只会落到 cluster[0] 的作用域里 ——
        # 等于把 A 的信息写进了 B（`NFR-M-03` 的越界）。
        groups: dict[tuple[str, str], list[MemoryItem]] = {}
        for item in members:
            groups.setdefault((item.tenant_id, item.scope), []).append(item)

        budget = self._limits.max_clusters_per_run
        for (tenant_id, scope), items in groups.items():
            clusters = find_clusters(
                items, threshold=policy.cluster_similarity, min_size=policy.min_cluster_size
            )
            for cluster in clusters:
                if budget <= 0:
                    return MaintenanceReport(distilled=distilled, rejected=rejected)
                budget -= 1
                ctx = MemoryContext(scope=cluster[0].scope, tenant_id=tenant_id)
                candidate = await self._reflector.distill(cluster, ctx=ctx)
                if candidate is None:
                    continue
                outcome = await self._store_candidate(store, candidate, ctx=ctx, now=now)
                if outcome.stored:
                    distilled += 1
                else:
                    rejected += 1
        return MaintenanceReport(distilled=distilled, rejected=rejected)

    async def _reflect(
        self, store: MemoryStore, now: datetime, last: datetime | None
    ) -> MaintenanceReport:
        policy = self._reflector.policy
        if not policy.enabled:
            return MaintenanceReport()
        since = last or (now - timedelta(hours=self._limits.reflect_window_hours))
        items = await store.trajectory(limit=policy.max_trajectory_items, since=since)
        if not items:
            return MaintenanceReport()

        # 同 :meth:`_distill`：**按 (租户, 作用域) 分组**，否则归纳出的经验会
        # 落到其中一簇的作用域里，而它的依据来自别的作用域。
        groups: dict[tuple[str, str], list[MemoryItem]] = {}
        for item in items:
            groups.setdefault((item.tenant_id, item.scope), []).append(item)

        reflected = rejected = 0
        for (tenant_id, scope), group in groups.items():
            ctx = MemoryContext(scope=scope, tenant_id=tenant_id)
            candidates = await self._reflector.reflect(group, ctx=ctx)
            for candidate in candidates:
                outcome = await self._store_candidate(store, candidate, ctx=ctx, now=now)
                if outcome.stored:
                    reflected += 1
                else:
                    rejected += 1
            if reflected:
                self._emit("reflected", {"count": reflected}, ctx, outcome="reflected")
        return MaintenanceReport(reflected=reflected, rejected=rejected)

    # ------------------------------------------------------------ 内部
    async def _append(
        self, store: MemoryStore, op: str, memory_id: UUID | None, *, before: str, after: str,
        reason: str, ctx: MemoryContext, now: datetime, payload: Mapping[str, Any] | None = None,
    ) -> None:
        await store.append_op(
            op_id=str(new_id()), op=op,
            memory_id=str(memory_id) if memory_id else None,
            before_hash=before[:128], after_hash=after[:128], reason=reason,
            actor=ctx.caller or "system", source_trace_id=ctx.trace_id,
            scope=ctx.scope, tenant_id=ctx.tenant_id, payload=dict(payload or {}), now=now,
        )

    def _emit(
        self, key: str, payload: Mapping[str, Any], ctx: MemoryContext, *,
        subject: str = "", outcome: str = "",
    ) -> None:
        name = DEFAULT_EVENTS.get(key, f"memory.{key}")
        body = {"scope": ctx.scope, "tenant_id": ctx.tenant_id, "session_id": ctx.session_id}
        body.update(subject=subject, outcome=outcome)
        body.update(payload)
        try:
            self._events.emit(name, body)
        except Exception as exc:  # noqa: BLE001 — 事件出口不该影响主流程
            _log.debug("记忆事件发送失败：%s", exc)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _aggregate(outcomes: Sequence[RememberOutcome]) -> RememberOutcome:
    """把多条候选的结果汇总成一个（`remember` 的返回）。"""
    stored = [outcome for outcome in outcomes if outcome.stored]
    rejected = [outcome for outcome in outcomes if outcome.rejected]
    total = len(outcomes)
    if stored:
        first = stored[0]
        return RememberOutcome(
            resolution=first.resolution,
            memory_id=first.memory_id,
            reason=f"{len(stored)}/{total} 条已写入；代表理由：{first.reason}",
            candidates=total,
        )
    if rejected:
        return RememberOutcome(
            None, rejected_gate=rejected[0].rejected_gate,
            reason=f"{len(rejected)}/{total} 条被闸门拒绝；代表理由：{rejected[0].reason}",
            candidates=total,
        )
    return RememberOutcome(None, reason="没有候选被写入", candidates=total)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

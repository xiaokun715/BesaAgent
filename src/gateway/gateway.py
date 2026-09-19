"""门面与责任链编排 —— 全平台模型调用的**唯一入口**。

**编排是一个方法，不是九层装饰器**（架构概要设计-gateway §7 B-3）。
理由是 §2.1 那张顺序表：``health`` 必须在 ``retry`` 之前、``retry`` 在 ``fallback`` 之内 ——
这些是**约束**而不是实现细节。散在九层中间件里，没有人能一眼看出顺序错了，
而且中间件链很难表达「health 失败要**跳过** retry」这种跳转。

**唯一的上游计数点是 :class:`CallBudget`**（``NFR-G-04``）。
本文件里**不允许出现第二个 attempt 计数器** —— 一旦出现，上界就不再可知。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from foundation.clock import Clock, SystemClock
from gateway.cost import CostSheet
from gateway.errors import (
    AllCandidatesFailedError,
    BudgetExhaustedError,
    GatewayError,
    StreamCommittedError,
)
from gateway.fallback import FallbackPolicy, decide
from gateway.health import HealthPolicy, HealthRegistry
from gateway.rate_limit import LocalRateLimiter, build_rate_limiter
from gateway.registry import Registry
from gateway.retry import CallBudget, RetryPolicy, compute_backoff, should_retry
from gateway.router import RoutingContext, select
from gateway.types import (
    AttemptRecord,
    GatewayResult,
    ModelSpec,
    StreamChunk,
    StreamDone,
    StreamEvent,
    StreamFailed,
)
from gateway.usage import UsageLedger, UsageRecord
from provider.base import ModelBase
from provider.errors import ProviderError
from provider.types import (
    Capability,
    ChatRequest,
    ChatResponse,
    EmbeddingResult,
    Message,
    ToolSpec,
    Usage,
    has_any_image,
)

__all__ = ["EventEmitter", "EventName", "Gateway", "NullEmitter"]

_log = logging.getLogger(__name__)

#: 粗略 token 估算：4 字符 ≈ 1 token。
#:
#: 只用于 **TPM 预扣**，不用于计费（计费一律用上游返回的实际值）。
#: 刻意不引入 tokenizer 依赖（``D-D``）：不同厂商用不同分词器，
#: 而 TPM 预扣只需要量级正确 —— 误差由 :meth:`LocalRateLimiter.reconcile` 回补。
_CHARS_PER_TOKEN = 4


class EventName:
    """事件名。**只有门面发事件**，子模块只「返回发生了什么」（``B-4``）。

    TODO: ``src/event`` 实现后，这些常量应移入 ``src/event/types.py``。
    放在这里是一期权宜 —— 但事件**语义**（谁发、字段含义）已经是最终形态。
    """

    CALL_STARTED = "gateway.call.started"
    CALL_SUCCEEDED = "gateway.call.succeeded"
    CALL_FAILED = "gateway.call.failed"
    CALL_RETRIED = "gateway.call.retried"
    CALL_DEGRADED = "gateway.call.degraded"
    MODEL_SKIPPED = "gateway.model.skipped"
    CIRCUIT_OPENED = "gateway.circuit.opened"
    QUOTA_EXHAUSTED = "gateway.quota.exhausted"


class EventEmitter(Protocol):
    """事件接收方。"""

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...


class NullEmitter:
    """默认发射器：什么都不做。

    默认值必须是静默的 —— 事件是**观测**，不该成为调用链路的依赖。
    """

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:
        return None


class Gateway:
    """模型调用门面。"""

    def __init__(
        self,
        registry: Registry,
        *,
        retry: RetryPolicy | None = None,
        fallback: FallbackPolicy | None = None,
        health: HealthRegistry | None = None,
        health_policy: HealthPolicy | None = None,
        rate_limit: LocalRateLimiter | None = None,
        ledger: UsageLedger | None = None,
        cost: CostSheet | None = None,
        events: EventEmitter | None = None,
        clock: Clock | None = None,
        deadline_s: float | None = 120.0,
    ) -> None:
        self._registry = registry
        self._clock: Clock = clock or SystemClock()
        self._retry = retry or RetryPolicy()
        self._fallback = fallback or FallbackPolicy()
        self._health = health or HealthRegistry(health_policy or HealthPolicy(), self._clock)
        self._limiter = rate_limit or build_rate_limiter(None, clock=self._clock)
        self._ledger = ledger or UsageLedger()
        self._cost = cost or CostSheet()
        self._events: EventEmitter = events or NullEmitter()
        self._deadline_s = deadline_s

        #: 模型实例缓存。键是 ``(模型键, 类型)``。
        #: **必须缓存**：每次 ``chat_model()`` 都新建实例虽然共享连接池，
        #: 但会重复解析能力集，而这是热路径。
        self._models: dict[tuple[str, str], ModelBase] = {}

    # ------------------------------------------------------------------ 对外
    async def chat(
        self,
        alias: str,
        messages: Sequence[Message],
        *,
        trace_id: str = "",
        session_id: str | None = None,
        caller: str | None = None,
        deadline_s: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[ToolSpec] | None = None,
        tool_choice: str | None = None,
        response_format: str | None = None,
        json_schema: Mapping[str, Any] | None = None,
        stop: Sequence[str] | None = None,
    ) -> GatewayResult:
        """对话补全。"""
        ctx = RoutingContext.for_request(
            stream=False,
            tools=bool(tools),
            vision=has_any_image(messages),
            json_output=response_format is not None,
            session_id=session_id,
            caller=caller,
            cost_of=self._cost_lookup,
            health=self._health,
        )
        budget = self._new_budget(deadline_s)

        # 请求体与候选无关（模型名由 provider 适配器自己填），所以构造一次即可。
        request = ChatRequest(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,  # type: ignore[arg-type]
            json_schema=json_schema,
            stop=stop,
            trace_id=trace_id,
        )

        async def invoke(spec: ModelSpec) -> ChatResponse:
            model = self._resolve_model(spec, "runtime")
            return await model.chat(request)  # type: ignore[attr-defined]

        return await self._execute(
            alias=alias,
            ctx=ctx,
            budget=budget,
            invoke=invoke,
            trace_id=trace_id,
            session_id=session_id,
            caller=caller,
            estimate=self._estimate_chat_tokens(messages),
        )

    async def embed(
        self,
        alias: str,
        texts: Sequence[str],
        *,
        trace_id: str = "",
        session_id: str | None = None,
        caller: str | None = None,
        deadline_s: float | None = None,
        batch_size: int | None = None,
    ) -> GatewayResult:
        """向量化。"""
        # 向量化的必需能力与对话不同（不含 CHAT），所以不走 for_request ——
        # 那是「对话请求形态 → 能力」的推导，不是万能的。
        ctx = RoutingContext(
            required=frozenset({Capability.EMBEDDING}),
            session_id=session_id,
            caller=caller,
            cost_of=self._cost_lookup,
            health=self._health,
            describes=("向量化",),
        )
        budget = self._new_budget(deadline_s)

        async def invoke(spec: ModelSpec) -> EmbeddingResult:
            model = self._resolve_model(spec, "embedding")
            return await model.embed(texts, batch_size=batch_size, trace_id=trace_id)  # type: ignore[attr-defined]

        return await self._execute(
            alias=alias,
            ctx=ctx,
            budget=budget,
            invoke=invoke,
            trace_id=trace_id,
            session_id=session_id,
            caller=caller,
            estimate=sum(len(text) for text in texts) // _CHARS_PER_TOKEN,
        )

    def stream_chat(
        self,
        alias: str,
        messages: Sequence[Message],
        *,
        trace_id: str = "",
        session_id: str | None = None,
        caller: str | None = None,
        deadline_s: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式对话。

        **返回迭代器而不是异步生成器函数**（架构概要设计-provider §4.1 的同一条理由）：
        ``async def`` + ``yield`` 会推迟到首次迭代才执行代码，
        于是「alias 拼错了」「没有能满足能力的模型」这类**配置错误**
        会发生在离调用点很远的地方。用普通 ``def`` 可以先同步完成解析与选链，
        错误在**调用瞬间**抛出。
        """
        ctx = RoutingContext.for_request(
            stream=True,
            vision=has_any_image(messages),
            session_id=session_id,
            caller=caller,
            cost_of=self._cost_lookup,
            health=self._health,
        )
        budget = self._new_budget(deadline_s)

        # 同步解析 + 选链：错误在这里抛，不在迭代时抛
        chain = self._plan(alias, ctx)

        def build() -> ChatRequest:
            return ChatRequest(
                messages=messages,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
                trace_id=trace_id,
            )

        return self._stream_impl(
            alias=alias,
            chain=chain,
            budget=budget,
            build=build,
            trace_id=trace_id,
            session_id=session_id,
            caller=caller,
            estimate=self._estimate_chat_tokens(messages),
        )

    # ------------------------------------------------------------------ 核心
    async def _execute(
        self,
        *,
        alias: str,
        ctx: RoutingContext,
        budget: CallBudget,
        invoke: Callable[[ModelSpec], Awaitable[Any]],
        trace_id: str,
        session_id: str | None,
        caller: str | None,
        estimate: int,
    ) -> GatewayResult:
        chain = self._plan(alias, ctx)
        attempts: list[AttemptRecord] = []

        self._emit(
            EventName.CALL_STARTED,
            {"alias": alias, "trace_id": trace_id, "candidates": [s.key for s in chain]},
        )

        for index, spec in enumerate(chain):
            remaining = len(chain) - index - 1
            outcome = await self._attempt_candidate(
                spec=spec,
                invoke=invoke,
                budget=budget,
                attempts=attempts,
                trace_id=trace_id,
                session_id=session_id,
                caller=caller,
                estimate=estimate,
            )
            if outcome is not None:
                result = self._finalize(
                    response=outcome,
                    alias=alias,
                    spec=spec,
                    attempts=attempts,
                    index=index,
                    trace_id=trace_id,
                    session_id=session_id,
                    caller=caller,
                )
                return result

            # 失败：问 fallback 要不要换下一个
            decision = decide(
                _last_error(attempts) or RuntimeError("候选失败"),
                policy=self._fallback,
                budget=budget,
                remaining_candidates=remaining,
                stream_committed=False,
            )
            if decision.proceed:
                self._emit(
                    EventName.CALL_DEGRADED,
                    {"alias": alias, "trace_id": trace_id, "from": spec.key, "reason": decision.reason},
                )
                continue

            self._raise_terminal(decision.reason, alias, attempts, trace_id)

        self._raise_terminal("no_more_candidates", alias, attempts, trace_id)

    async def _attempt_candidate(
        self,
        *,
        spec: ModelSpec,
        invoke: Callable[[ModelSpec], Awaitable[Any]],
        budget: CallBudget,
        attempts: list[AttemptRecord],
        trace_id: str,
        session_id: str | None,
        caller: str | None,
        estimate: int,
    ) -> Any | None:
        """尝试一个候选（含重试）。成功返回结果，失败返回 ``None``。

        **顺序是约束**（架构概要设计-gateway §2.1）：

        1. 熔断拦截 —— 发生在申请配额**之前**，所以它不消耗重试次数；
        2. 限流申请；
        3. 重试循环，每次尝试都向 ``budget`` 申请配额。

        **``probe_outstanding`` 的语义**：``health.allow()`` 在 HALF_OPEN 下会占一个
        探测位，**无论走哪条出路都必须归还**（记成功、记失败、或直接 release）。
        用 ``finally`` 兜底，是为了让将来新增的异常分支不会静默泄漏探测位 ——
        泄漏的后果是模型永远恢复不到 CLOSED，且日志上看不出来。
        """
        # 1. 熔断
        if not self._health.allow(spec.key):
            attempts.append(
                AttemptRecord(
                    model_key=spec.key,
                    provider=spec.provider,
                    outcome="skipped",
                    skipped_reason="circuit_open",
                )
            )
            self._emit(EventName.MODEL_SKIPPED, {"model": spec.key, "reason": "circuit_open"})
            return None

        # 2. 限流
        decision = await self._limiter.acquire(spec.key, estimated_tokens=estimate, budget=budget)
        if decision.denied:
            # 放行了熔断却没用上，探测位必须归还 —— 否则半开状态永远恢复不了
            self._health.release(spec.key)
            attempts.append(
                AttemptRecord(
                    model_key=spec.key,
                    provider=spec.provider,
                    outcome="skipped",
                    skipped_reason=f"rate_limited:{decision.reason}",
                )
            )
            self._emit(
                EventName.QUOTA_EXHAUSTED,
                {"model": spec.key, "reason": decision.reason},
            )
            return None

        probe_outstanding = True
        try:
            retry_index = 0
            while True:
                if not budget.try_acquire():
                    # 预算耗尽：**这是唯一的计数点**，走了这一支就说明不能再发请求了
                    attempts.append(
                        AttemptRecord(
                            model_key=spec.key,
                            provider=spec.provider,
                            outcome="skipped",
                            skipped_reason="budget_exhausted",
                        )
                    )
                    self._health.release(spec.key)
                    probe_outstanding = False
                    break

                attempt_started = self._clock.monotonic()
                try:
                    response = await invoke(spec)
                except asyncio.CancelledError:
                    # 取消既不是成功也不是失败：模型没出错，是我们的调用方不想等了。
                    # 记成失败会污染熔断计数 —— 连续几次用户取消就能把一个健康模型熔断掉。
                    self._health.release(spec.key)
                    probe_outstanding = False
                    raise
                except ProviderError as exc:
                    exc.with_trace(trace_id)
                    self._health.record_failure(spec.key)
                    probe_outstanding = False
                    attempts.append(
                        AttemptRecord(
                            model_key=spec.key,
                            provider=spec.provider,
                            outcome="failed",
                            error=str(exc),
                            retryable=exc.retryable,
                            elapsed_s=self._clock.monotonic() - attempt_started,
                        )
                    )
                    self._record_failure_usage(spec, trace_id, session_id, caller)
                    self._emit(
                        EventName.CALL_FAILED,
                        {"model": spec.key, "error": str(exc), "retryable": exc.retryable},
                    )

                    # 两个上限都要判，缺一不可：
                    #   - should_retry 看**全局预算**（跨候选累计）
                    #   - retry_index  看**单候选上限**
                    # 只判前者会让「一个候选吃掉全部预算」——
                    # 降级链后面的候选永远轮不到，而配置里写的 max_attempts_per_candidate
                    # 变成一个没人读的数字。
                    if not should_retry(exc, budget) or (
                        retry_index >= self._retry.max_attempts_per_candidate - 1
                    ):
                        break

                    delay = compute_backoff(retry_index, self._retry)

                    # **退避等待也必须受总预算约束**（FR-G-12）。
                    # 不判这一条，`重试 3 次 × 退避 2^n` 可以把 10 秒的 deadline
                    # 撑成 21 秒 —— 指数退避的后几跳必然超过任何合理的 deadline，
                    # 于是「总超时」在长尾路径上形同虚设。
                    # 等不完的退避等于必然超时，此时应立刻结束这个候选。
                    if delay > budget.remaining_s():
                        break

                    retry_index += 1
                    self._emit(
                        EventName.CALL_RETRIED,
                        {"model": spec.key, "attempt": retry_index, "delay_s": delay},
                    )
                    await self._clock.sleep(delay)
                    continue
                else:
                    self._health.record_success(spec.key)
                    probe_outstanding = False
                    attempts.append(
                        AttemptRecord(
                            model_key=spec.key,
                            provider=spec.provider,
                            outcome="success",
                            elapsed_s=self._clock.monotonic() - attempt_started,
                        )
                    )
                    return response
        finally:
            if probe_outstanding:
                # 兜底归还：任何未在上面分支覆盖的退出路径都不该泄漏探测位。
                self._health.release(spec.key)
            # 并发额度**必须**在 finally 里释放 —— 取消路径上最容易漏，
            # 而漏一个并发位意味着那个模型永久少一个并发额度（FR-G-13 验收点）。
            self._limiter.release(spec.key)

        return None

    # ------------------------------------------------------------------ 流式
    async def _stream_impl(
        self,
        *,
        alias: str,
        chain: list[ModelSpec],
        budget: CallBudget,
        build: Callable[[], ChatRequest],
        trace_id: str,
        session_id: str | None,
        caller: str | None,
        estimate: int,
    ) -> AsyncIterator[StreamEvent]:
        attempts: list[AttemptRecord] = []
        #: 是否因预算不足而提前停下，以及在哪个候选上停下 ——
        #: 收场时用它区分「全部候选都失败了」与「还有候选没试过」。
        budget_blocked = False
        blocked_at = 0

        for index, spec in enumerate(chain):
            remaining = len(chain) - index - 1

            if not self._health.allow(spec.key):
                attempts.append(
                    AttemptRecord(spec.key, spec.provider, "skipped", skipped_reason="circuit_open")
                )
                continue

            decision = await self._limiter.acquire(
                spec.key, estimated_tokens=estimate, budget=budget
            )
            if decision.denied:
                self._health.release(spec.key)
                attempts.append(
                    AttemptRecord(
                        spec.key, spec.provider, "skipped",
                        skipped_reason=f"rate_limited:{decision.reason}",
                    )
                )
                continue

            probe_outstanding = True
            try:
                if not budget.try_acquire():
                    attempts.append(
                        AttemptRecord(
                            spec.key, spec.provider, "skipped", skipped_reason="budget_exhausted"
                        )
                    )
                    budget_blocked = True
                    blocked_at = index
                    break

                model = self._resolve_model(spec, "runtime")
                chunks: list[str] = []
                try:
                    # stream_chat 的校验在**调用瞬间**执行（provider 层设计），
                    # 所以能力不匹配会在这里同步抛错，而不是迭代到一半。
                    stream = model.stream_chat(build())  # type: ignore[attr-defined]
                except ProviderError as exc:
                    exc.with_trace(trace_id)
                    self._health.record_failure(spec.key)
                    probe_outstanding = False
                    attempts.append(
                        AttemptRecord(
                            spec.key, spec.provider, "failed",
                            error=str(exc), retryable=exc.retryable,
                        )
                    )
                    if decide(
                        exc,
                        policy=self._fallback,
                        budget=budget,
                        remaining_candidates=remaining,
                        stream_committed=False,
                    ).proceed:
                        continue
                    yield StreamFailed(error=exc, attempts=tuple(attempts))
                    return

                try:
                    async for piece in stream:
                        chunks.append(piece)
                        yield StreamChunk(text=piece)
                except asyncio.CancelledError:
                    self._health.release(spec.key)
                    probe_outstanding = False
                    raise
                except ProviderError as exc:
                    exc.with_trace(trace_id)
                    self._health.record_failure(spec.key)
                    probe_outstanding = False
                    attempts.append(
                        AttemptRecord(
                            spec.key, spec.provider, "failed",
                            error=str(exc), retryable=exc.retryable,
                        )
                    )

                    # 只有**一个分片都没吐出去**时才允许降级（FR-G-05）。
                    if not chunks and decide(
                        exc,
                        policy=self._fallback,
                        budget=budget,
                        remaining_candidates=remaining,
                        stream_committed=False,
                    ).proceed:
                        continue

                    # 已输出过正文：禁止降级与重试。
                    # 用户已经看到半截答案，重发只会得到第二份不连贯的输出。
                    error: GatewayError = (
                        StreamCommittedError(
                            f"流式已输出 {len(chunks)} 个分片后失败，无法重试或降级：{exc}",
                            alias=alias,
                            trace_id=trace_id,
                            attempts=tuple(attempts),
                        )
                        if chunks
                        else exc
                    )
                    yield StreamFailed(error=error, attempts=tuple(attempts))
                    return
                else:
                    self._health.record_success(spec.key)
                    probe_outstanding = False
                    attempts.append(AttemptRecord(spec.key, spec.provider, "success"))
                    # **流式路径拿不到用量**：``ChatModel.stream_chat`` 的契约是
                    # 「逐段产出正文」（``AsyncIterator[str]``），没有承载 usage 的位置。
                    # 所以这里构造的 ChatResponse 用量为全 ``None`` —— 于是成本也是
                    # 「未知」而不是 0（``FR-G-09`` 的 ``None`` 语义在这里救了场：
                    # 错误地记成 0 会让流式调用的成本在报表里凭空消失）。
                    #
                    # 要真正拿到流式用量，需要 OpenAI 的
                    # ``stream_options: {"include_usage": true}`` **并且**
                    # 让 stream_chat 能回传结束时的 usage —— 那是契约变更，
                    # 见《架构概要设计-provider》§4.1 关于流式返回形状的讨论。一期不做。
                    response = ChatResponse(
                        content="".join(chunks),
                        model=spec.model,
                        finish_reason="stop",
                        trace_id=trace_id,
                    )
                    yield StreamDone(
                        result=self._finalize(
                            response=response,
                            alias=alias,
                            spec=spec,
                            attempts=attempts,
                            index=index,
                            trace_id=trace_id,
                            session_id=session_id,
                            caller=caller,
                        )
                    )
                    return
            finally:
                if probe_outstanding:
                    self._health.release(spec.key)
                # 生成器被提前关闭（调用方 break / 取消）时也会走到这里，
                # 并发额度因此不会泄漏。
                self._limiter.release(spec.key)

        # 与 ``fallback.decide`` 同一条规则：**还有候选没试过却停了**才算预算耗尽。
        # 候选走完了就是「全部失败」，否则排障方向会跑偏到调预算上去。
        exhausted = budget.exhausted_reason()
        ran_out_of_candidates = not budget_blocked or (len(chain) - blocked_at - 1) <= 0
        terminal: GatewayError = (
            AllCandidatesFailedError(
                "所有候选均失败", alias=alias, trace_id=trace_id, attempts=tuple(attempts)
            )
            if exhausted is None or ran_out_of_candidates
            else BudgetExhaustedError(
                reason=exhausted, alias=alias, attempts=tuple(attempts), trace_id=trace_id
            )
        )
        yield StreamFailed(error=terminal, attempts=tuple(attempts))

    # ------------------------------------------------------------------ 辅助
    def _plan(self, alias: str, ctx: RoutingContext) -> list[ModelSpec]:
        """解析 alias → 选链。**同步**，因此 ``UnknownAliasError`` /
        ``NoCapableModelError`` 会在调用瞬间抛出，而不是等第一次迭代。"""
        alias_spec = self._registry.alias(alias)
        return select(self._registry.candidates(alias), ctx, alias_spec.strategy)

    def _resolve_model(self, spec: ModelSpec, kind: str) -> ModelBase:
        cache_key = (spec.key, kind)
        model = self._models.get(cache_key)
        if model is not None:
            return model

        provider = self._registry.provider(spec.key)
        model = (
            provider.chat_model() if kind == "runtime" else provider.embedding_model()
        )
        self._models[cache_key] = model
        return model

    def _finalize(
        self,
        *,
        response: Any,
        alias: str,
        spec: ModelSpec,
        attempts: list[AttemptRecord],
        index: int,
        trace_id: str,
        session_id: str | None,
        caller: str | None,
    ) -> GatewayResult:
        usage = getattr(response, "usage", None) or Usage()
        cost = self._cost.calc(spec.key, usage)
        degraded = index > 0

        self._ledger.record(
            UsageRecord.from_parts(
                usage=usage,
                cost=cost,
                trace_id=trace_id,
                alias=alias,
                model_key=spec.key,
                provider=spec.provider,
                model=spec.model,
                session_id=session_id,
                caller=caller,
                mono_at=self._clock.monotonic(),
                degraded=degraded,
                attempt_index=len(attempts),
            )
        )

        if degraded:
            _log.warning(
                "发生降级：alias=%s 实际使用 %s（第 %d 个候选）trace=%s",
                alias, spec.key, index + 1, trace_id,
            )

        self._emit(
            EventName.CALL_SUCCEEDED,
            {
                "alias": alias,
                "model": spec.key,
                "trace_id": trace_id,
                "degraded": degraded,
                "cost": str(cost),
            },
        )

        return GatewayResult(
            response=response,
            alias=alias,
            model_key=spec.key,
            attempts=tuple(attempts),
            degraded=degraded,
            usage=usage,
            cost=cost,
            trace_id=trace_id,
        )

    def _record_failure_usage(
        self,
        spec: ModelSpec,
        trace_id: str,
        session_id: str | None,
        caller: str | None,
    ) -> None:
        """失败也要记账。

        **用量字段留 ``None`` 而不是 0**：我们不知道上游有没有计费、计了多少。
        但「某个模型被打过一次」这个事实本身是有价值的 ——
        账单对不上时，它是唯一的线索。补 0 会让这条线索消失。
        """
        self._ledger.record(
            UsageRecord(
                trace_id=trace_id,
                alias="",
                model_key=spec.key,
                provider=spec.provider,
                model=spec.model,
                session_id=session_id,
                caller=caller,
                mono_at=self._clock.monotonic(),
            )
        )

    def _new_budget(self, deadline_s: float | None) -> CallBudget:
        effective = self._deadline_s if deadline_s is None else deadline_s
        return CallBudget(
            max_attempts=self._retry.total_max_attempts,
            clock=self._clock,
            deadline_s=effective,
        )

    def _cost_lookup(self, model_key: str) -> Any:
        """供 ``cost`` 路由策略取价。缺价返回 ``None``（会被排到最后，不是当成 0）。"""
        price = self._cost.price_of(model_key)
        return None if price is None else price.input + price.output

    @staticmethod
    def _estimate_chat_tokens(messages: Sequence[Message]) -> int:
        total = 0
        for message in messages:
            content = message.content
            total += len(content) if isinstance(content, str) else sum(
                len(part.text) for part in content if hasattr(part, "text")
            )
        return max(1, total // _CHARS_PER_TOKEN)

    def _raise_terminal(
        self,
        reason: str,
        alias: str,
        attempts: list[AttemptRecord],
        trace_id: str,
    ) -> None:
        """走到链尾或预算耗尽时的收场。``reason`` 决定错误类型与修法提示。"""
        if reason in ("attempts", "deadline"):
            raise BudgetExhaustedError(
                reason=reason, alias=alias, attempts=tuple(attempts), trace_id=trace_id
            )
        if reason == "stream_committed":
            raise StreamCommittedError("流式已输出正文，无法重试或降级", alias=alias, trace_id=trace_id)
        raise AllCandidatesFailedError(
            f"所有候选均失败（{reason}）",
            alias=alias,
            trace_id=trace_id,
            attempts=tuple(attempts),
        )

    def _emit(self, name: str, payload: Mapping[str, Any]) -> None:
        """发事件。**绝不让观测影响主流程**（``FR-G-10``）。"""
        try:
            self._events.emit(name, payload)
        except Exception:  # noqa: BLE001 - 观测失败不能拖垮调用
            _log.debug("事件发射失败：%s", name, exc_info=True)

    async def aclose(self) -> None:
        """释放全部 provider 连接。**幂等**。"""
        self._models.clear()
        await self._registry.aclose()

    # ------------------------------------------------------------------ 观测
    @property
    def ledger(self) -> UsageLedger:
        return self._ledger

    @property
    def health(self) -> HealthRegistry:
        return self._health

    @property
    def limiter(self) -> LocalRateLimiter:
        return self._limiter


def _last_error(attempts: Sequence[AttemptRecord]) -> Exception | None:
    """取最近一次失败，供 fallback 决策判断。"""
    for record in reversed(attempts):
        if record.outcome == "failed":
            return RuntimeError(record.error or "候选失败")
    return None

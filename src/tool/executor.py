"""执行编排 —— 本模块唯一的入口。

责任链（**顺序是约束，不是实现细节**）：

```text
① 找得到这个工具吗      找不到 → 拒绝（列出全部可用名）
② 参数合法吗            不合法 → 拒绝（**绝不「尽力执行」**）
③ 注入检测通过吗        不通过 → 拒绝（路径穿越 / 内网地址 / 敏感路径）
④ 权限允许吗            不允许 → 拒绝（**不登记、不执行**）
⑤ 调用链会成环吗        成环 / 超深 / 超总次数 → 拒绝（把调用栈回给模型）
⑥ 幂等：这个键做过吗    做过 → 复用结果（副作用不再发生）
⑦ 登记 in_flight        必须先于执行
⑧ 沙箱执行
⑨ 分类失败 → 自愈       参数错给建议 / 瞬时故障在幂等安全时重试一次
⑩ 结果处置              脱敏 → 截断 / 落盘
```

**③ 在 ④ 之前**：权限回答的是「你有没有资格碰这个范围」，
注入回答的是「这个参数本身就不可信」—— 两件事。
混在一起判，会让一个路径穿越的参数先去走一遍权限逻辑，而那条逻辑假定参数格式合法。

**④ 在 ⑥ 之前** —— 这一点与需求说明书的初稿相反，是编码阶段推翻的。初稿的理由
（「重复的调用会因为无权限被拒，而上层以为没执行过」）漏了一件事：

    幂等在权限**之后**时，一个**未被授权**的调用者只要猜到（或复用）别人的幂等键，
    就能拿到**别人**执行出来的结果 —— 比如它无权读的文件内容。
    去重表于是变成了一个**越权读取的通道**。

**⑦ 必须先于 ⑧**：反过来的话，崩在「执行完但没登记」之间，
下次重试会**再执行一遍**，而系统完全不知道发生过。
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from foundation.logging import current_trace_id
from tool.base import Tool, ToolContext
from tool.idempotency import Claim, IdempotencyGuard, args_digest
from tool.injection import Inspector
from tool.permission import PermissionDecision, PermissionPolicy
from tool.recovery import RetryBreaker, classify
from tool.registry import ToolRegistry
from tool.result import ResultProcessor
from tool.types import SideEffect, ToolInvocation, ToolResult

__all__ = ["CallStack", "EventSink", "ToolEvent", "ToolExecutor"]

_log = logging.getLogger(__name__)

#: 默认的「一次运行内工具调用总次数」上界。
#:
#: 它不是环检测 —— 它是**给不可控的增长加一个可读的上界**，与 ``CallBudget`` 完全同一条思路。
#: 模型可以不打环，但仍然可以在两万个不同的工具上各调一次。
DEFAULT_MAX_CALLS_PER_RUN = 200
DEFAULT_MAX_CALL_DEPTH = 8


class EventSink(Protocol):
    """事件出口的形状（一个 ``emit(name, payload)``）。

    **为什么不 import ``gateway.gateway.EventEmitter``**：
    那是 gateway 的类型，而 tool 在依赖链上比 gateway 低半层 ——
    让 tool 反向 import 它会让两者绑在一起（改 gateway 的事件协议会牵动 tool）。
    这里只需要一个**结构协议**，``BufferingEmitter`` 天然满足。
    """

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...


class ToolEvent:
    """工具事件名。**这个模块只有一个事件出口**（``ToolExecutor``）。

    ⚠ 与 ``gateway.py`` 里的 ``EventName`` 是同一件事的两个副本 ——
    两者将来都要搬进 ``src/event/types.py``。``src/event`` 至今未实现，
    而为一个字符串词汇表先建一个模块，会把「事件总线怎么设计」这个更大的问题
    提前引爆，所以两处各自持有，**搬的时候要一起搬**（``T-N``）。
    """

    STARTED = "tool.started"
    EXECUTED = "tool.executed"
    EXECUTED_DEGRADED = "tool.executed_degraded"
    REUSED = "tool.reused"
    REFUSED = "tool.refused"
    UNCERTAIN = "tool.uncertain"
    SPILLED = "tool.spilled"
    LOOP_BLOCKED = "tool.loop_blocked"


@dataclass
class CallStack:
    """当前协程链上的工具调用栈，外加一次运行的调用计数。

    **用 ``contextvars`` 而不是实例状态**：工具执行是并发的（多 agent），
    实例上的一个 ``list`` 会被另一个协程看见 —— 那会**拦掉合法的并发调用**，
    而症状是「偶尔有工具莫名其妙被拒」，离原因很远。
    这与 ``foundation.logging`` 用 contextvar 承载 trace_id 是同一个手法。

    ``contextvars`` 天然按协程链隔离：同一个父任务里嵌套调用看得见彼此的栈
    （那正是「环」的定义），而并发的兄弟任务各看各的。
    """

    max_depth: int = DEFAULT_MAX_CALL_DEPTH
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN

    _stack: contextvars.ContextVar[tuple[str, ...]] = field(
        default_factory=lambda: contextvars.ContextVar("besa_tool_stack", default=())
    )
    _calls: contextvars.ContextVar[int] = field(
        default_factory=lambda: contextvars.ContextVar("besa_tool_calls", default=0)
    )

    # ---------------------------------------------------------------- 读
    @property
    def stack(self) -> tuple[str, ...]:
        return self._stack.get()

    @property
    def depth(self) -> int:
        return len(self._stack.get())

    @property
    def calls_made(self) -> int:
        return self._calls.get()

    def check(self, tool_name: str) -> str:
        """能不能进这个工具。返回拒绝原因；空串表示放行。"""
        stack = self.stack
        if tool_name in stack:
            return (
                f"检测到工具调用成环：{' → '.join((*stack, tool_name))}\n"
                "调用栈里已经出现过这个工具 —— 按这条路径走下去会无限循环。"
                "换一条路径，或改掉让它们互相调用的那个判断。"
            )
        if len(stack) >= self.max_depth:
            return (
                f"工具调用深度已达上界（{self.max_depth}）：{' → '.join(stack)}\n"
                "不是环，但没有底 —— 同样会走不到头。"
            )
        if self.calls_made >= self.max_calls_per_run:
            return (
                f"本次运行的工调用次数已达上界（{self.max_calls_per_run}）。\n"
                "这是**给不可控的增长加的上界**，不是环检测 —— "
                "模型可以不打环，但仍然可以在两万个不同的工具上各调一次。"
            )
        return ""

    # ---------------------------------------------------------------- 写
    def enter(self, tool_name: str) -> contextvars.Token:
        """进栈 + 计数。返回值交给 :meth:`exit`。

        ⚠ **计数只增不减**：``exit`` 只复位**栈**，不复位计数 ——
        否则「一次运行内的总次数」永远回到 0，那条上界就形同虚设
        （这是一个真实踩过的 bug：走到第 3 次时计数还是 0）。
        计数按「一次运行」重置，由 :meth:`reset` 显式做。
        """
        self._calls.set(self._calls.get() + 1)
        return self._stack.set((*self._stack.get(), tool_name))

    def exit(self, token: contextvars.Token) -> None:
        self._stack.reset(token)

    def reset(self) -> None:
        """开一次新的运行。**由调用方在运行开始时显式调**。"""
        self._stack.set(())
        self._calls.set(0)


@dataclass(frozen=True)
class ToolExecutor:
    """把注册表、权限、幂等、注入检测、沙箱、自愈、结果处置接到一起。

    **它不是 frozen 里塞不下的那种「无状态」** —— ``breaker`` 与 ``calls``
    是有状态的，所以它们各自内部做了协程安全的处理（见各自的 docstring）。
    """

    registry: ToolRegistry
    permission: PermissionPolicy
    guard: IdempotencyGuard

    inspector: Inspector = field(default_factory=Inspector)
    results: ResultProcessor = field(default_factory=ResultProcessor)
    breaker: RetryBreaker = field(default_factory=RetryBreaker)
    calls: CallStack = field(default_factory=CallStack)

    events: EventSink | None = None
    #: 工具入参里承载显式幂等键的字段名
    key_field: str = "idempotency_key"
    limits: Mapping[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- 入口
    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        """执行一次工具调用。**这是本模块唯一的对外入口。**"""
        started = time.monotonic()

        # ① 找工具
        try:
            tool = self.registry.get(invocation.tool_name)
        except LookupError as exc:
            return self._refuse(invocation, "read", str(exc))

        definition = tool.definition()
        effect = definition.side_effect

        # ② 参数
        arguments, invalid = self._prepare_arguments(invocation, definition.parameters)
        if invalid is not None:
            return self._refuse(invocation, effect, invalid, reason="参数不合法")

        ctx = self.permission.context_for(
            definition,
            scope=invocation.scope,
            trace_id=invocation.trace_id,
            session_id=invocation.session_id,
            caller=invocation.caller,
            limits=self.limits,
        )

        # ③ 注入检测（**在权限之前** —— 见模块 docstring）
        verdict = self.inspector.check(arguments, allowed_paths=ctx.allowed_paths)
        if not verdict.safe:
            return self._refuse(invocation, effect, verdict.reason(), reason="注入检测")

        # ④ 权限
        decision: PermissionDecision = self.permission.authorize(definition)
        if not decision.allowed:
            return self._refuse(invocation, effect, decision.reason, reason="未授权")

        # ⑤ 循环 / 深度 / 总次数
        blocked = self.calls.check(invocation.tool_name)
        if blocked:
            self._emit(ToolEvent.LOOP_BLOCKED, invocation, effect, outcome="refused")
            return self._refuse(invocation, effect, blocked, reason="循环")

        # ⑤.5 无效重试熔断 —— 独立于幂等的一道防线
        fingerprint = args_digest(invocation)
        if self.breaker.should_refuse(fingerprint):
            return self._refuse(
                invocation,
                effect,
                f"同一份参数已经连续失败 {self.breaker.count(fingerprint)} 次。\n"
                "**换个做法** —— 重复提交同样的参数不会有不同结果。",
                reason="无效重试",
            )

        # ⑥⑦ 幂等：判定 + 登记
        claim: Claim = await self.guard.claim(invocation, effect)
        if not claim.can_execute:
            self._emit(
                self._event_for(claim.outcome), invocation, effect,
                outcome=claim.outcome, reason=claim.reason,
            )
            return claim.as_result()

        if claim.degraded:
            self._emit(
                ToolEvent.EXECUTED_DEGRADED, invocation, effect,
                outcome="executed_degraded", reason=claim.reason,
            )

        # ⑧ 执行
        self._emit(ToolEvent.STARTED, invocation, effect, outcome="started")
        tokens = self.calls.enter(invocation.tool_name)
        try:
            result = await tool.run(arguments, ctx)
        except Exception as exc:  # noqa: BLE001
            result = self._on_exception(exc, tool=tool, invocation=invocation)
        finally:
            self.calls.exit(tokens)

        # ⑨ 收尾：分类、自愈、结果处置
        return await self._finish(
            result, claim=claim, invocation=invocation, ctx=ctx, fingerprint=fingerprint,
            started=started, degraded=claim.degraded,
        )

    # ---------------------------------------------------------------- 内部
    async def _finish(
        self,
        result: ToolResult,
        *,
        claim: Claim,
        invocation: ToolInvocation,
        ctx: ToolContext,
        fingerprint: str,
        started: float,
        degraded: bool,
    ) -> ToolResult:
        effect = claim.side_effect

        if result.ok:
            self.breaker.clear(fingerprint)
            # **结果处置在登记之前**：落盘与脱敏都要发生，
            # 而登记进权威记录的是**处置之后**的东西（脱敏不可逆，不能先存后脱）。
            result = await self.results.process(
                result, ctx=ctx, idem_key=claim.idem_key
            )
        else:
            self.breaker.record_failure(fingerprint)

        elapsed = time.monotonic() - started
        outcome = result.outcome
        # **降级标记不能丢**：工具只知道「我执行成功了」，它不知道这次是在
        # 没有幂等保护的情况下被放行的 —— 直接覆盖会让那个事实消失。
        if degraded and outcome == "executed":
            outcome = "executed_degraded"

        result = ToolResult(
            outcome=outcome,
            tool_name=result.tool_name,
            output=result.output,
            error=result.error,
            truncated=result.truncated,
            elapsed_s=elapsed,
            idem_key=claim.idem_key,
            spilled_path=result.spilled_path,
            redacted=result.redacted,
            error_kind=result.error_kind,
            recovery=result.recovery,
            hint=result.hint,
        )

        if result.outcome in {"executed", "executed_degraded"}:
            await self.guard.mark_done(claim, result)
        else:
            await self.guard.mark_failed(claim, result.error or result.outcome)

        if result.spilled_path:
            self._emit(
                ToolEvent.SPILLED, invocation, effect,
                outcome=result.outcome, path=result.spilled_path,
            )

        self._emit(
            ToolEvent.EXECUTED if result.ok else ToolEvent.REFUSED,
            invocation, effect,
            outcome=result.outcome,
            elapsed_s=round(elapsed, 3),
            truncated=result.truncated,
            redacted=result.redacted,
        )
        return result

    def _on_exception(
        self, exc: BaseException, *, tool: Tool, invocation: ToolInvocation
    ) -> ToolResult:
        """把异常变成一个**失败结果**，并给出自愈建议。

        **不得冒泡成 agent 崩溃**（`NFR-T-04`）：一个工具崩了不该让整轮执行停下，
        它应当变成一个失败结果回给模型，让模型换个做法。
        """
        _log.exception("工具 %s 执行时抛出未处理的异常", tool.name)
        recovery = classify(
            exc, message=str(exc), side_effect=tool.side_effect
        )
        return ToolResult(
            outcome="failed",
            tool_name=tool.name,
            error=f"{type(exc).__name__}: {exc}",
            error_kind=recovery.kind,
            recovery=recovery.action,
            hint=recovery.hint,
        )

    def _prepare_arguments(
        self, invocation: ToolInvocation, schema: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """把调用参数整理成工具实参，或返回一条拒绝原因。

        **显式幂等键要从实参里剔除** —— 它是执行层的概念，不是工具的入参。
        不剔除的话，有严格 schema 的工具会因为「多了一个未知参数」而校验失败，
        而那个失败的形状（「参数不符」）会把人引向错误的方向。
        """
        args = dict(invocation.arguments)
        args.pop(self.key_field, None)

        try:
            import jsonschema
        except ImportError:  # pragma: no cover - 依赖已在 pyproject 里声明
            return args, None

        if not schema:
            return args, None

        try:
            jsonschema.validate(instance=args, schema=dict(schema))
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "（根）"
            # **位置必须给具体**：「schema 不符」模型改不动，
            # 「第 3 个参数缺 path」它能改。
            return args, (
                f"参数不符合 {invocation.tool_name} 的 schema：{exc.message}（位置：{path}）\n"
                "参数校验失败时**不会执行** —— 带着错参数执行会产生更难查的副作用。\n"
                f"该工具的必填参数：{schema.get('required') or '（无）'}"
            )
        except jsonschema.SchemaError as exc:
            return args, f"工具 {invocation.tool_name} 的 schema 本身有问题：{exc.message}"

        return args, None

    def _refuse(
        self,
        invocation: ToolInvocation,
        effect: SideEffect,
        error: str,
        *,
        reason: str = "refused",
    ) -> ToolResult:
        self._emit(
            ToolEvent.REFUSED, invocation, effect, outcome="refused", reason=reason
        )
        return ToolResult(outcome="refused", tool_name=invocation.tool_name, error=error)

    @staticmethod
    def _event_for(outcome: str) -> str:
        if outcome == "reused":
            return ToolEvent.REUSED
        if outcome == "uncertain":
            return ToolEvent.UNCERTAIN
        return ToolEvent.REFUSED

    def _emit(
        self,
        name: str,
        invocation: ToolInvocation,
        effect: SideEffect,
        **extra: Any,
    ) -> None:
        """发事件。**绝不让观测影响主流程**（与 gateway 的 ``_emit`` 同一条纪律）。"""
        if self.events is None:
            return

        trace_id = invocation.trace_id or current_trace_id()
        if trace_id == "-":
            trace_id = ""

        payload: dict[str, Any] = {
            "tool_name": invocation.tool_name,
            "subject": invocation.tool_name,
            "side_effect": effect,
            "trace_id": trace_id,
            "session_id": invocation.session_id,
            "caller": invocation.caller,
            **{k: v for k, v in extra.items() if v is not None},
        }
        try:
            self.events.emit(name, payload)
        except Exception:  # noqa: BLE001 - 观测失败不能拖垮工具调用
            _log.debug("工具事件发射失败：%s", name, exc_info=True)

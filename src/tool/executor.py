"""执行编排 —— 本模块唯一的入口。

责任链（顺序是约束，不是实现细节）：

```text
① 找得到这个工具吗      找不到 → 拒绝（列出全部可用名）
② 参数合法吗            不合法 → 拒绝（**绝不「尽力执行」**）
③ 权限允许吗            不允许 → 拒绝（**不登记、不执行**）
④ 幂等：这个键做过吗    做过 → 复用结果（副作用不再发生）
⑤ 登记 in_flight        必须先于执行
⑥ 执行
⑦ 登记 done / failed
```

**③ 在 ④ 之前 —— 这一点与需求说明书的初稿相反，是编码阶段推翻的**（见
`docs/tool/架构概要设计-tool.md` 的实现期修订）。初稿的顺序是「幂等在前」，
理由是「重复的调用会因为无权限被拒，而上层以为没执行过」。
但那条理由漏了一件事：

    权限判定在幂等**之后**时，一个**未被授权**的调用者只要猜到（或复用）
    别人的幂等键，就能拿到**别人**执行出来的结果 —— 比如它无权读的文件内容。

也就是说「幂等在前」把去重表变成了一个**越权读取的通道**。
而被拒的重复调用并不会造成实际损害：那个调用者本来就不该跑这个工具。

**⑦ 的失败登记不是终态**：``failed`` 之后可以重试（重新登记），
这与 ``uncertain`` 不同 —— 后者是「不知道副作用发生没有」，工具层不替上层赌。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from foundation.logging import current_trace_id
from tool.base import Tool, ToolContext
from tool.idempotency import Claim, IdempotencyGuard
from tool.permission import PermissionDecision, PermissionPolicy
from tool.registry import ToolRegistry
from tool.types import SideEffect, ToolInvocation, ToolResult

__all__ = ["EventSink", "ToolExecutor", "ToolEvent"]

_log = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ToolExecutor:
    """把注册表、权限、幂等、事件接到一起。

    **它没有内部状态**（``frozen=True``）—— 每次执行需要的东西都从参数进来，
    所以一个实例可以被并发使用，也不需要在关停时释放什么。
    """

    registry: ToolRegistry
    permission: PermissionPolicy
    guard: IdempotencyGuard
    events: EventSink | None = None
    #: 工具入参里承载显式幂等键的字段名
    key_field: str = "idempotency_key"
    limits: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.limits is None:
            object.__setattr__(self, "limits", {})

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        """执行一次工具调用。**这是本模块唯一的对外入口。**"""
        started = time.monotonic()

        # ① 找工具
        try:
            tool = self.registry.get(invocation.tool_name)
        except LookupError as exc:
            return ToolResult(
                outcome="refused", tool_name=invocation.tool_name, error=str(exc)
            )

        definition = tool.definition()

        # ② 参数
        arguments, invalid = self._prepare_arguments(invocation, definition.parameters)
        if invalid is not None:
            self._emit(
                ToolEvent.REFUSED,
                invocation,
                definition.side_effect,
                outcome="refused",
                reason="参数不合法",
            )
            return ToolResult(
                outcome="refused", tool_name=tool.name, error=invalid
            )

        # ③ 权限（**在幂等之前** —— 见模块 docstring）
        decision: PermissionDecision = self.permission.authorize(definition)
        if not decision.allowed:
            self._emit(
                ToolEvent.REFUSED,
                invocation,
                definition.side_effect,
                outcome="refused",
                reason=decision.reason,
            )
            return ToolResult(
                outcome="refused", tool_name=tool.name, error=decision.reason
            )

        # ④⑤ 幂等：判定 + 登记（挂在同一个调用上，见 idempotency.py）
        claim: Claim = await self.guard.claim(invocation, definition.side_effect)
        if not claim.can_execute:
            result = claim.as_result()
            self._emit(
                ToolEvent.REUSED if claim.outcome == "reused" else ToolEvent.UNCERTAIN
                if claim.outcome == "uncertain"
                else ToolEvent.REFUSED,
                invocation,
                definition.side_effect,
                outcome=claim.outcome,
                reason=claim.reason,
            )
            return result

        if claim.degraded:
            # **没有幂等保护**地放行了（只读工具 + Redis 不可用）。
            # 用独立的事件名与 outcome，否则「偶尔出现的重复执行」查不出原因。
            self._emit(
                ToolEvent.EXECUTED_DEGRADED,
                invocation,
                definition.side_effect,
                outcome="executed_degraded",
                reason=claim.reason,
            )

        # ⑥ 执行
        ctx = self.permission.context_for(
            definition,
            scope=invocation.scope,
            trace_id=invocation.trace_id,
            session_id=invocation.session_id,
            caller=invocation.caller,
            limits=self.limits,
        )
        self._emit(
            ToolEvent.STARTED, invocation, definition.side_effect, outcome="started"
        )

        try:
            result = await tool.run(arguments, ctx)
        except Exception as exc:  # noqa: BLE001
            # 工具实现里的意外异常**不得冒泡成 agent 崩溃**（`NFR-T-04`）：
            # 它变成一个失败结果回给模型，让模型换个做法。
            error = f"{type(exc).__name__}: {exc}"
            _log.exception("工具 %s 执行时抛出未处理的异常", tool.name)
            await self.guard.mark_failed(claim, error)
            self._emit(
                ToolEvent.REFUSED,
                invocation,
                definition.side_effect,
                outcome="failed",
                reason=error,
            )
            return ToolResult(
                outcome="failed",
                tool_name=tool.name,
                error=error,
                elapsed_s=time.monotonic() - started,
                idem_key=claim.idem_key,
            )

        # ⑦ 收尾
        elapsed = time.monotonic() - started
        # **降级标记不能丢**：工具只知道「我执行成功了」，它不知道这次是
        # 在没有幂等保护的情况下被放行的。那是 claim 才知道的事 ——
        # 用工具返回的 outcome 直接覆盖，会让「这次没有幂等保护」这个事实
        # 在结果里消失，而它正是「偶尔出现的重复执行」唯一的线索。
        outcome = result.outcome
        if claim.degraded and outcome == "executed":
            outcome = "executed_degraded"

        result = ToolResult(
            outcome=outcome,
            tool_name=tool.name,
            output=result.output,
            error=result.error,
            truncated=result.truncated,
            elapsed_s=elapsed,
            idem_key=claim.idem_key,
        )

        if result.outcome in {"executed", "executed_degraded"}:
            await self.guard.mark_done(claim, result)
        else:
            # refused / failed 都是「没做成」，要允许重试
            await self.guard.mark_failed(claim, result.error or result.outcome)

        self._emit(
            ToolEvent.EXECUTED if result.ok else ToolEvent.REFUSED,
            invocation,
            definition.side_effect,
            outcome=result.outcome,
            elapsed_s=round(elapsed, 3),
            truncated=result.truncated,
        )
        return result

    # ---------------------------------------------------------------- 辅助
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
            return args, (
                f"参数不符合 {invocation.tool_name} 的 schema：{exc.message}（位置：{path}）\n"
                "参数校验失败时**不会执行** —— 带着错参数执行会产生更难查的副作用。"
            )
        except jsonschema.SchemaError as exc:
            return args, f"工具 {invocation.tool_name} 的 schema 本身有问题：{exc.message}"

        return args, None

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

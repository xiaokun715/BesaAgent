"""工具层：契约、注册、权限、幂等与执行编排。

**这一层解决的问题**：模型只能产生**意图**（「我要调用 ``read``，参数是 path=...」），
把意图变成真实世界的副作用是这里的职责。而真正的难点不是「能跑」，是
**「同一个调用，无论被提交几次，副作用只发生一次」**。

**与 `src/skill` 的分工**（容易混）：

- ``src/tool`` 是能执行的**代码** —— ``read`` / ``grep`` / ``write`` / ``bash``；
- ``src/skill`` 是可持久化、带版本的**数据** —— 能力定义与编排。

技能要执行动作时，通过本模块执行。

**依赖方向（硬约束）**：本模块只依赖 ``foundation`` 与 ``repo``。
**不得** import ``provider``（契约 4 机械强制）、``gateway``、``agent``。
厂商的工具调用协议是传输层的事；工具定义是本模块自己的类型，
转换成厂商要的 ``ToolSpec`` 发生在**组合根**（只有它同时看得见两边）。

**执行入口只有一个**：``ToolExecutor.invoke()``。它把顺序写死在一处 ——
校验 → 权限 → 幂等 → 登记 → 执行 → 收尾。绕开它直接调工具，
就等于绕开了幂等与权限。
"""

from __future__ import annotations

from tool.base import Tool, ToolContext, truncate
from tool.catalog import Catalog, CatalogIssue, Embedder
from tool.executor import CallStack, EventSink, ToolEvent, ToolExecutor
from tool.idempotency import (
    Claim,
    ClaimState,
    IdempotencyGuard,
    IdempotencyStore,
    StoreUnavailable,
    UnavailableStore,
    args_digest,
    derive_key,
)
from tool.injection import InjectionPolicy, Inspector
from tool.permission import PermissionDecision, PermissionPolicy
from tool.recovery import Recovery, RetryBreaker, classify
from tool.registry import ToolRegistry, UnknownToolError, build_default_registry
from tool.result import ResultPolicy, ResultProcessor
from tool.sandbox import ProcessOutcome, Sandbox, SandboxLimits
from tool.types import (
    ErrorKind,
    InjectionKind,
    InjectionVerdict,
    Outcome,
    RecoveryAction,
    SideEffect,
    ToolDefinition,
    ToolInvocation,
    ToolResult,
)

__all__ = [
    "CallStack",
    "Catalog",
    "CatalogIssue",
    "Claim",
    "ClaimState",
    "Embedder",
    "ErrorKind",
    "EventSink",
    "IdempotencyGuard",
    "IdempotencyStore",
    "InjectionKind",
    "InjectionPolicy",
    "InjectionVerdict",
    "Inspector",
    "Outcome",
    "PermissionDecision",
    "PermissionPolicy",
    "ProcessOutcome",
    "Recovery",
    "RecoveryAction",
    "ResultPolicy",
    "ResultProcessor",
    "RetryBreaker",
    "Sandbox",
    "SandboxLimits",
    "SideEffect",
    "StoreUnavailable",
    "Tool",
    "ToolContext",
    "ToolDefinition",
    "ToolEvent",
    "ToolExecutor",
    "ToolInvocation",
    "ToolRegistry",
    "ToolResult",
    "UnavailableStore",
    "UnknownToolError",
    "args_digest",
    "build_default_registry",
    "classify",
    "derive_key",
    "truncate",
]

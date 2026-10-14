"""单 agent 的运行时层：把「一次任务」跑完，并且跑得**可判定、可解释、可续跑**。

**它解决的问题**：一次运行有二十步、会调工具、会花几分钟，而它会在任何一步被打断。
打断留下的**不是一个干净的状态** —— 「工具执行过了，但结果没回填」这件事
必须被显式对待，否则续跑会安静地重复产生副作用。

**四层分工**（读代码前先记住）：

==============================  ==================================================
:mod:`agent.executor`           **一次循环**：想 → 调工具 → 再想（纯循环，不认识上下文）
:mod:`agent.runtime`            **一次运行**：路由 / 预算 / 续跑判定 / 运行内压缩 / 收尾
:mod:`agent.router`             任务 → 哪个阶段（显式 → 规则 → LLM → 可配的兜底）
:mod:`agent.stages`             七个阶段是**数据**（新增阶段不改代码）
==============================  ==================================================

**依赖方向（硬约束）**：本模块依赖 ``context`` / ``memory``（经 context）/
``tool`` / ``gateway`` / ``repo`` / ``foundation``。
**不得** import ``provider``（import-linter 机械强制）、``apps``、``multiagent`` ——
是 multiagent 把单 agent 当**零件**用，不是反过来。

**对外只有一个面**：:class:`agent.runtime.AgentRuntime`。
"""

from __future__ import annotations

from agent.base import AgentResult, SimpleStageAgent, StageAgent
from agent.executor import AgentExecutor, PrepareHook
from agent.router import RoutingDecision, StageRouter, UnknownPolicy
from agent.runtime import (
    DEFAULT_EVENTS,
    AgentLimits,
    AgentPolicy,
    AgentRuntime,
    ContextRunPolicy,
    EventSink,
    NullEventSink,
    ResumePolicy,
    RunOutcome,
)
from agent.stages import (
    BUILTIN_STAGES,
    STAGE_ORDER,
    StageDefinition,
    StageRegistry,
    UnknownStageError,
    build_stages,
)
from agent.state import RUN_STATUSES, AgentStep, RunStatus, TaskRun

__all__ = [
    "BUILTIN_STAGES",
    "DEFAULT_EVENTS",
    "RUN_STATUSES",
    "STAGE_ORDER",
    "AgentExecutor",
    "AgentLimits",
    "AgentPolicy",
    "AgentResult",
    "AgentRuntime",
    "AgentStep",
    "ContextRunPolicy",
    "EventSink",
    "NullEventSink",
    "PrepareHook",
    "ResumePolicy",
    "RoutingDecision",
    "RunOutcome",
    "RunStatus",
    "SimpleStageAgent",
    "StageAgent",
    "StageDefinition",
    "StageRegistry",
    "StageRouter",
    "TaskRun",
    "UnknownPolicy",
    "UnknownStageError",
    "build_stages",
]

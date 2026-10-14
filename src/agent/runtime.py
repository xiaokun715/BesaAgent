"""单 agent 的**运行时层** —— 一次运行的编排（`docs/agent`）。

**它管什么**：路由到哪个阶段、这次运行总共能花多少、续跑安不安全、
每一轮给模型看什么、收尾要留哪些痕。

**不管什么**：循环本身（那是 :mod:`agent.executor`）、工具怎么执行（:mod:`tool`）、
模型怎么选（:mod:`gateway`）、历史怎么压（:mod:`context`）。

## 本模块存在的理由：**「跑到一半」必须是一个可判定的状态**

工具层已经把「不知道副作用发生没有」标出来了（`in_flight` 超租约 → `uncertain`），
而它的文档明确写着「那是**上层**的决定」。**这个上层就是这里** ——
在它存在之前，续跑只看检查点，于是「重复写一份用例文件」会在续跑时安静地发生。

## 第二个理由：运行内也会超窗口

一次运行内部的消息**全部属于同一「轮」**（只有一条 user 消息），
所以上下文层默认的按轮切分**一步都压不掉**。而工具结果的体量是实打实的
（单条大结果落盘上限 64KB）。所以这里在每一轮调模型之前，把消息按**「步」**
交给上下文层压一次 —— 见 :meth:`AgentRuntime._prepare`。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from foundation.clock import Clock, SystemClock
from foundation.logging import current_trace_id

from agent.base import AgentResult, StageAgent
from agent.executor import AgentExecutor
from agent.router import RoutingDecision, StageRouter
from agent.stages import StageRegistry
from agent.state import RunStatus, TaskRun
# ``context`` **顶层导入**：它是个必然可用的包（在 ``pyproject`` 的 root_packages 里），
# 真正"可选"的是**实例**（``context=None``），不是**模块**。
# 之前写成函数内 import，是想表达"没有它也能跑"，但那只让阅读时要来回跳 ——
# 同一个文件里 repo / gateway 是顶层、context 是函数内，两套写法没有理由。
from context.builder import from_message, rebuild
from context.compact.types import CompactResult, blocks_from_messages
from context.context import ContextRequest
from context.types import ContextMessage
from gateway.messages import Message, system, user
from repo.checkpoint import NEEDS_REVIEW_STATUS, CheckpointRepo
from repo.tool import ToolExecutionRepo

__all__ = [
    "DEFAULT_EVENTS",
    "AgentLimits",
    "AgentPolicy",
    "AgentRuntime",
    "ContextRunPolicy",
    "EventSink",
    "NullEventSink",
    "ResumePolicy",
    "RunOutcome",
]

_log = logging.getLogger(__name__)


class EventSink(Protocol):
    """事件出口。形状与 gateway / memory / context 的出口一致（``emit(name, payload)``）。"""

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...


class NullEventSink:
    """默认出口。**不是静默，而是让"没接事件"是一个明示的选择。**"""

    __slots__ = ()

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:  # noqa: D102
        return None


#: 一次运行的事件（架构概要设计 §8）。``<模块>.<对象>.<动作>``。
DEFAULT_EVENTS = {
    "started": "agent.run.started",
    "routed": "agent.stage.routed",
    "resume_decided": "agent.resume.decided",
    "step": "agent.step.completed",
    "compacted": "agent.context.compacted",
    "budget_exhausted": "agent.budget.exhausted",
    "finished": "agent.run.finished",
    "degraded": "agent.degraded",
}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentLimits:
    """三个上界（`FR-A-05` / `C-A-4`）。**没有"不限"这个选项** —— 可以调大，不能不写。"""

    max_steps: int = 20
    #: **整轮**运行的时间预算。不是单次调用的 —— 那个归 gateway（`C-A-3`）
    deadline_s: float | None = 600.0
    #: 成本上界。``None`` = 不做成本熔断（不是"不限金额"，只是不在这里拦）
    max_cost: Decimal | None = None
    #: 单次调用的时长上限
    call_timeout_s: float | None = None
    #: 整轮工具调用次数上界（工具层还有一层，这里是顶层兜底）
    max_tool_calls: int = 100

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "AgentLimits":
        data = dict(cfg or {})
        raw_cost = data.get("max_cost")
        limits = cls(
            max_steps=int(data.get("max_steps", 20)),
            deadline_s=None if data.get("deadline_s") is None else float(data["deadline_s"]),
            max_cost=None if raw_cost is None else Decimal(str(raw_cost)),
            call_timeout_s=(
                None if data.get("call_timeout_s") is None else float(data["call_timeout_s"])
            ),
            max_tool_calls=int(data.get("max_tool_calls", 100)),
        )
        if limits.max_steps <= 0:
            raise ValueError(f"agent.limits.max_steps 必须为正，得到 {limits.max_steps}")
        if limits.deadline_s is not None and limits.deadline_s <= 0:
            raise ValueError(f"agent.limits.deadline_s 必须为正或 null，得到 {limits.deadline_s}")
        return limits


@dataclass(frozen=True)
class ResumePolicy:
    """续跑遇到「不确定的工具执行」时怎么办（`FR-A-10` / `C-A-2`）。"""

    #: **默认最保守** —— 宁可停下问一句，不可默默重做。
    #:
    #: - ``needs_review``：交给上层/人决策（默认）
    #: - ``abort``：放弃，不冒险
    #: - ``retry_uncertain``：**明确冒险**重做那一步（必须显式配，且会告警）
    on_uncertain: Literal["needs_review", "abort", "retry_uncertain"] = "needs_review"
    #: 判「超租约」的阈值，与 ``idempotency.lease_s`` 对齐
    lease_s: float = 120.0

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "ResumePolicy":
        data = dict(cfg or {})
        mode = str(data.get("on_uncertain", "needs_review"))
        if mode not in ("needs_review", "abort", "retry_uncertain"):
            raise ValueError(
                f"agent.resume.on_uncertain 只能是 needs_review | abort | retry_uncertain，"
                f"得到 {mode!r}"
            )
        return cls(on_uncertain=mode, lease_s=float(data.get("lease_s", 120.0)))  # type: ignore[arg-type]


@dataclass(frozen=True)
class ContextRunPolicy:
    """运行内的上下文（`FR-A-08` / `FR-A-09`）。"""

    enabled: bool = True
    #: 最近几步**不参与压缩**。**必须 ≥ 1** —— 当前步永远要完整可见，
    #: 否则模型看不到自己刚调了什么、拿到了什么。
    keep_recent_steps: int = 4
    #: 每轮把消息压到"模型可用输入"的这个比例以内
    target_ratio: float = 0.6

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "ContextRunPolicy":
        data = dict(cfg or {})
        policy = cls(
            enabled=bool(data.get("enabled", True)),
            keep_recent_steps=int(data.get("keep_recent_steps", 4)),
            target_ratio=float(data.get("target_ratio", 0.6)),
        )
        if policy.keep_recent_steps < 1:
            raise ValueError(
                "agent.context.keep_recent_steps 必须 ≥ 1 —— "
                "当前这一步必须完整可见，否则模型看不到自己刚做了什么"
            )
        if not 0.0 < policy.target_ratio <= 1.0:
            raise ValueError(
                f"agent.context.target_ratio 必须在 (0,1]，得到 {policy.target_ratio}"
            )
        return policy


@dataclass(frozen=True)
class AgentPolicy:
    """``agent`` 段的整体。"""

    alias: str = "runtime.default"
    limits: AgentLimits = field(default_factory=AgentLimits)
    resume: ResumePolicy = field(default_factory=ResumePolicy)
    context: ContextRunPolicy = field(default_factory=ContextRunPolicy)
    emit_events: bool = True

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "AgentPolicy":
        data = dict(cfg or {})
        return cls(
            alias=str(data.get("alias") or "runtime.default"),
            limits=AgentLimits.from_config(data.get("limits")),
            resume=ResumePolicy.from_config(data.get("resume")),
            context=ContextRunPolicy.from_config(data.get("context")),
            emit_events=bool(data.get("emit_events", True)),
        )


# --------------------------------------------------------------------------- #
# 产出
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunOutcome:
    """一次运行的产出。**自足** —— 上层（含 multiagent）不需要别的上下文就能理解它。"""

    run_id: str
    stage: str = ""
    status: RunStatus = "running"
    answer: str = ""
    steps: int = 0
    tool_calls: int = 0
    elapsed_s: float = 0.0
    #: ``None`` = 价格未知（`FR-G-09`），不是 0
    cost: Decimal | None = None
    error: str = ""
    #: 为什么停下 / 为什么需要人看 —— **不带理由的状态等于没有状态**
    reason: str = ""
    routing: RoutingDecision | None = None

    @property
    def ok(self) -> bool:
        return self.status == "done"

    @property
    def needs_review(self) -> bool:
        return self.status == NEEDS_REVIEW_STATUS

    @property
    def cost_text(self) -> str:
        return "未知" if self.cost is None else str(self.cost)

    def __str__(self) -> str:
        # **只用 ASCII 标记**。中文 Windows 的 GBK 控制台编不出 ``✓`` / ``⚠``，
        # `print` 会直接抛 ``UnicodeEncodeError`` —— 一次演示、
        # 甚至一行日志，就能把一次运行炸掉。
        # 状态的区分交给方括号里那个词，不靠符号。
        mark = {"done": "OK", "failed": "!!", "cancelled": "--", "needs_review": "??"}.get(
            self.status, ".."
        )
        detail = self.error or self.reason or self.answer[:40] or "-"
        return (
            f"{mark} {self.stage} [{self.status}] {self.steps} 步 / {self.tool_calls} 次工具 "
            f"/ {self.elapsed_s:.1f}s / 花费 {self.cost_text}：{detail}"
        )


@dataclass
class _Budget:
    """一次运行的花费账。**用单调时钟** —— 墙钟会被校时影响（`clock.py` 的纪律）。"""

    limits: AgentLimits
    clock: Clock
    started: float = 0.0
    cost: Decimal = Decimal("0")
    cost_known: bool = True
    tool_calls: int = 0

    def start(self) -> None:
        self.started = self.clock.monotonic()

    def elapsed(self) -> float:
        return max(0.0, self.clock.monotonic() - self.started)

    def remaining_s(self) -> float | None:
        if self.limits.deadline_s is None:
            return None
        return self.limits.deadline_s - self.elapsed()

    def add_cost(self, amount: Decimal | None) -> None:
        if amount is None:
            self.cost_known = False
        else:
            self.cost += amount

    @property
    def total_cost(self) -> Decimal | None:
        return self.cost if self.cost_known else None

    def stop_reason(self, *, cancelled: bool) -> str:
        """该停了吗。**返回非空就是停下的理由，且理由里要写明撞的是哪一个上界。**"""
        if cancelled:
            return "运行被取消（外部要求停下）"
        if self.limits.deadline_s is not None and self.remaining_s() is not None:
            if self.remaining_s() <= 0:  # type: ignore[operator]
                return f"达到时间预算（{self.limits.deadline_s:.0f} 秒）"
        if self.tool_calls >= self.limits.max_tool_calls:
            return f"达到工具调用次数上界（{self.limits.max_tool_calls}）"
        if self.limits.max_cost is not None:
            total = self.total_cost
            if total is not None and total >= self.limits.max_cost:
                return f"达到成本上界（{self.limits.max_cost}）"
        return ""


# --------------------------------------------------------------------------- #
# 运行时
# --------------------------------------------------------------------------- #


@dataclass
class AgentRuntime:
    """单个 agent 的运行时。**由组合根装配**，也可被 multiagent 当零件调用。

    ⚠ **它不持有 gateway**。调模型的是 :class:`~agent.executor.AgentExecutor`，
    查窗口的是上下文层（``ContextManager``）—— 运行时层只把预算与钩子递下去。
    曾经这里有一个 ``gateway`` 字段，但**从头到尾没被用过一次**：
    一个不用的依赖会让人误以为"这一层在调模型"，而真正调模型的地方在别处。
    """

    executor: AgentExecutor
    stages: StageRegistry
    router: StageRouter
    #: 上下文层。``None`` 时退回 executor 的最小拼法（``system + user``）
    context: Any | None = None
    #: 持久化执行入口（查检查点、查工具执行记录）。``None`` = 不落库
    database: Any | None = None
    policy: AgentPolicy = field(default_factory=AgentPolicy)
    clock: Clock = field(default_factory=SystemClock)
    events: Any | None = None

    #: 被要求取消的运行。**进程内**：跨进程的取消属于上层编排的事（`AQ-5`）
    _cancelled: set[str] = field(default_factory=set, init=False)
    #: 没有 repo 时的进程内历史（`FR-A-07`）
    _history: dict[str, list[Any]] = field(default_factory=dict, init=False)
    _warned_no_repo: bool = field(default=False, init=False)

    # ---------------------------------------------------------------- 入口
    async def run_one(
        self,
        task: str,
        *,
        stage: str | None = None,
        session_id: str | None = None,
        caller: str | None = None,
        resume: bool = False,
        run_id: str = "",
        history: Sequence[Any] = (),
    ) -> RunOutcome:
        """跑一个阶段到一个终态。**自足**：不知道自己在单跑还是被编排（`NFR-A-02`）。"""
        run_id = run_id or uuid.uuid4().hex
        budget = _Budget(self.policy.limits, self.clock)
        budget.start()

        self._emit("started", {"run_id": run_id, "session_id": session_id, "resume": resume})
        try:
            return await self._run(run_id, task, budget, stage=stage,
                                   session_id=session_id, caller=caller, resume=resume,
                                   history=history)
        except asyncio.CancelledError:
            # **取消不是崩溃**：留下快照（可续跑），并且**继续向上传播** ——
            # 吞掉它会让"停止"这个语义在某些框架里彻底失效（`NFR-A-07`）。
            await self._save_terminal(run_id, "cancelled", session_id=session_id, caller=caller)
            self._emit("finished", {"run_id": run_id, "status": "cancelled", "cancelled": True})
            raise

    async def _run(
        self, run_id: str, task: str, budget: _Budget, *, stage, session_id, caller, resume, history
    ) -> RunOutcome:
        # ① 路由
        decision = await self.router.route(task, stage=stage)
        self._emit(
            "routed",
            {
                "run_id": run_id,
                "stage": decision.stage,
                "reason": decision.reason,
                "confidence": round(decision.confidence, 3),
                "needs_input": decision.needs_input,
                "candidates": list(decision.candidates),
            },
        )
        if decision.needs_input:
            # **"需要指定阶段"是一种正常结果，不是错误** —— 但它确实需要人（或调用方）介入。
            return RunOutcome(
                run_id=run_id, status=NEEDS_REVIEW_STATUS, reason=decision.reason,
                routing=decision, elapsed_s=budget.elapsed(),
            )

        agent = self.stages.get(decision.stage)

        # ② 续跑判定
        resumed, kind, reason = await self._decide_resume(run_id, resume=resume)
        self._emit(
            "resume_decided",
            {"run_id": run_id, "decision": kind, "reason": reason},
        )
        if kind == "needs_review":
            await self._save_terminal(run_id, NEEDS_REVIEW_STATUS, session_id=session_id,
                                      caller=caller, stage=agent.stage, reason=reason)
            return RunOutcome(
                run_id=run_id, stage=agent.stage, status=NEEDS_REVIEW_STATUS,
                reason=reason, routing=decision, elapsed_s=budget.elapsed(),
            )
        if kind == "abort":
            await self._save_terminal(run_id, "failed", session_id=session_id, caller=caller,
                                      stage=agent.stage, reason=reason)
            return RunOutcome(
                run_id=run_id, stage=agent.stage, status="failed", error=reason,
                routing=decision, elapsed_s=budget.elapsed(),
            )

        # ③ 准备这一轮的消息（跨轮历史 + 当前任务），交给上下文层
        prepared = await self._initial_messages(
            agent, task, resumed=resumed, history=history,
            session_id=session_id, caller=caller,
        )

        # ④ 把预算与运行内压缩**按这次运行**传给循环。
        #
        # ⚠ **不能挂在执行器实例上**：那是个长生命周期对象，而 multiagent 会**并发**
        # 跑好几个单 agent —— 两个并发运行会互相覆盖对方的钩子与预算，
        # 表现是「A 的运行用了 B 的预算」，且**偶尔**发生、极难复现。
        def _progress(amount: Decimal | None, tool_calls: int) -> None:
            # 计数只有循环内做得到（一次模型调用一条结果、一批工具一批调用），
            # 而上界只有这里知道怎么算 —— 所以由循环回报、由这里判定。
            budget.add_cost(amount)
            budget.tool_calls = tool_calls

        result: AgentResult = await self.executor.run(
            agent, task, run_id=run_id, session_id=session_id, caller=caller,
            resume=False,          # 续跑已经在这里决定过了
            messages=prepared,
            prepare=(
                self._prepare
                if (self.policy.context.enabled and self.context is not None)
                else None
            ),
            remaining_s=budget.remaining_s,
            stop_reason=lambda: budget.stop_reason(cancelled=run_id in self._cancelled),
            call_timeout_s=self.policy.limits.call_timeout_s,
            on_progress=_progress,
        )

        # ⑤ 收尾
        status: RunStatus = result.status
        if run_id in self._cancelled and status == "failed":
            # 取消是通过 ``stop_reason`` 让循环停下的，循环只知道"停了" ——
            # **为什么停**由这里改写（`MA-7` 的同一条：状态要能区分）。
            status = "cancelled"

        if status == "failed" and result.error:
            self._emit("budget_exhausted", {"run_id": run_id, "reason": result.error})

        outcome = RunOutcome(
            run_id=run_id, stage=result.stage, status=status, answer=result.answer,
            steps=result.steps, tool_calls=result.tool_calls, error=result.error,
            cost=result.cost if result.cost is not None else budget.total_cost,
            elapsed_s=result.elapsed_s or budget.elapsed(),
            routing=decision,
        )
        await self._finish(outcome, session_id=session_id, caller=caller)
        return outcome

    async def run_turn(
        self,
        session_id: str,
        user_input: str,
        *,
        stage: str | None = None,
        caller: str | None = None,
    ) -> RunOutcome:
        """多轮对话里的一轮：跑一次任务，并把这一轮记进会话历史。"""
        history = list(self._session_history(session_id))
        outcome = await self.run_one(
            user_input, stage=stage, session_id=session_id, caller=caller, history=history
        )
        if outcome.ok:
            history.extend(_exchange(user_input, outcome.answer))
            self._history[session_id] = history
        return outcome

    def cancel(self, run_id: str) -> None:
        """要求某个运行停下（`AQ-5`）。

        **它是"另一个协程在喊停"，不是"立刻终止"** —— 循环会在下一轮检查点退出。
        外层直接取消协程（``CancelledError``）也走同一条收尾路径。
        """
        self._cancelled.add(run_id)

    # ---------------------------------------------------------------- 续跑判定
    async def _decide_resume(
        self, run_id: str, *, resume: bool
    ) -> tuple[TaskRun | None, str, str]:
        """返回 ``(可续的运行, 决策, 理由)``。

        决策取值：``start``（从头跑）/ ``resume``（接着跑）/ ``needs_review`` / ``abort``。
        """
        if not resume:
            return None, "start", "未要求续跑"
        if self.database is None:
            return None, "start", "没有数据库，无法查快照 —— 从头跑"

        async with self.database.transaction() as tx:
            view = await CheckpointRepo(tx).latest(run_id)
        if view is None:
            # **不是错误**：上次什么都没做成，从头跑就是对的。
            return None, "start", "没有找到这个 run_id 的快照 —— 从头跑"
        if view.status == NEEDS_REVIEW_STATUS:
            return None, "needs_review", (
                f"这个运行上次停在 {NEEDS_REVIEW_STATUS}，还没被人处理过。"
                "**先处理它，再决定要不要续跑** —— 直接重跑会盖掉那个待决的问题。"
            )
        if view.finished:
            # 已完成的不续跑：那不叫续跑，叫重跑一个做完的任务。
            return None, "start", f"上一次已经 {view.status}，这次是重跑"

        # ``CheckpointView`` 不带 session_id —— 调用方（上层）比快照更清楚它是谁。
        run = TaskRun.from_state(run_id, dict(view.state))

        uncertain = await self._uncertain_calls(run_id, run)
        if not uncertain:
            return run, "resume", f"从第 {run.next_index + 1} 步接着跑（无不确定的工具执行）"

        mode = self.policy.resume.on_uncertain
        detail = "、".join(uncertain[:3]) + ("…" if len(uncertain) > 3 else "")
        if mode == "retry_uncertain":
            # **明确冒险**：配置里写了这个值就意味着"我知道可能重复，我接受"。
            _log.warning(
                "续跑 %s：有 %d 个工具执行结果不明（%s），按 agent.resume.on_uncertain="
                "retry_uncertain **冒险重做**。这可能重复产生副作用。",
                run_id, len(uncertain), detail,
            )
            return run, "resume", f"**冒险重做**：{len(uncertain)} 个工具执行结果不明（{detail}）"
        if mode == "abort":
            return None, "abort", (
                f"有 {len(uncertain)} 个工具执行结果不明（{detail}），按配置放弃续跑。"
            )
        return None, "needs_review", (
            f"上一次停在第 {run.next_index + 1} 步，而这一步有 {len(uncertain)} 个工具调用"
            f"**执行过、但结果没回填**（{detail}）。\n"
            "接着跑可能重复产生副作用，重来可能白做 —— **这个取舍不替你做**。\n"
            "处理方式：确认那些调用是否已生效，然后选"
            " agent.resume.on_uncertain=retry_uncertain（冒险重做）或 abort（放弃）。"
        )

    async def _uncertain_calls(self, run_id: str, run: TaskRun) -> list[str]:
        """查这一步用过的工具调用里，哪些处于「不确定」。**判定由工具层做。**"""
        if self.database is None:
            return []
        keys = [key for step in run.steps for key in step.idem_keys]
        if not keys:
            # **没有键就查不了**（`AQ-2`）：此时把整步当作"无记录"而不是"不确定" ——
            # 说"不确定"会把本来就正常的续跑也拦下来。
            return []
        uncertain: list[str] = []
        async with self.database.transaction() as tx:
            repo = ToolExecutionRepo(tx)
            for key in keys:
                view = await repo.find(
                    run_id, key, lease_s=self.policy.resume.lease_s
                )
                if view is not None and view.uncertain:
                    uncertain.append(f"{view.tool_name or '?'}({key[:12]})")
        return uncertain

    # ---------------------------------------------------------------- 上下文
    async def _initial_messages(
        self, agent: StageAgent, task: str, *, resumed, history, session_id, caller
    ) -> Sequence[Message]:
        """准备这一轮要发的消息。**有上下文层时由它决定看什么。**"""
        current = agent.build_prompt(task, resumed=resumed)
        if self.context is None:
            return [system(agent.system_prompt), user(current)]

        request = ContextRequest(
            session_id=session_id or "",
            history=tuple(history),
            current_input=current,
            system=agent.system_prompt,
            goal=task,
            caller=caller,
            trace_id=self._trace_id(),
        )
        built = await self.context.build(request)
        return tuple(built.messages)

    async def _prepare(
        self, messages: Sequence[Message], step: int, remaining_s: float | None
    ) -> Sequence[Message]:
        """**运行内压缩**（`FR-A-08`）：每一轮调模型之前把消息压一次。

        两个刻意的做法：

        1. **开头那一段钉住不动**：一条运行的消息序列开头通常是任务本身
           （也可能是 system）—— 把它压掉等于 agent 忘了自己要干什么。
           上下文层的切分函数会把它单独分成一组，这里把它摘出来放在压缩之外；
        2. **按「步」切**（`unit="step"`）：一次运行内部的消息**全部属于同一轮**，
           按轮切一步都压不掉（`MA-2`）。
        """
        policy = self.policy.context
        if self.context is None or not policy.enabled:
            return messages

        converted = [from_message(message) for message in messages]
        preamble: list[Any] = []
        rest = list(converted)
        # 开头的非 assistant 消息 = 任务陈述（或 system）—— **钉住不压**。
        while rest and rest[0].role != "assistant":
            preamble.append(rest.pop(0))

        if not rest:
            return messages

        result = await self.context.compact_transcript(
            rest,
            keep_recent=policy.keep_recent_steps,
            unit="step",
            ratio=policy.target_ratio,
            reason=f"运行内压缩（第 {step + 1} 步之前）",
        )
        self._emit(
            "compacted",
            {
                "step": step + 1,
                "level": result.level_used,
                "saved_tokens": sum(entry.saved for entry in result.entries),
                "blocks": len(result.blocks),
                "note": result.note,
            },
        )

        blocks = (*blocks_from_messages(tuple(preamble), segment="window", level="L0"),
                  *result.blocks)
        return rebuild(blocks)

    # ---------------------------------------------------------------- 收尾
    async def _finish(self, outcome: RunOutcome, *, session_id, caller) -> None:
        """四件**旁路**收尾事（`FR-A-11`）。**任何一件失败都不得让运行失败。**"""
        await self._save_terminal(
            outcome.run_id, outcome.status, session_id=session_id, caller=caller,
            stage=outcome.stage,
        )
        self._emit(
            "finished",
            {
                "run_id": outcome.run_id, "stage": outcome.stage, "status": outcome.status,
                "steps": outcome.steps, "tool_calls": outcome.tool_calls,
                "elapsed_s": round(outcome.elapsed_s, 3), "cost": outcome.cost_text,
                "error": outcome.error, "reason": outcome.reason,
            },
        )

    async def _save_terminal(
        self, run_id: str, status: str, *, session_id, caller, stage: str = "", reason: str = ""
    ) -> None:
        """写最后一条快照。**失败只告警** —— 但它是四条里最要紧的一条（下次续跑靠它）。"""
        if self.database is None:
            return
        try:
            async with self.database.transaction() as tx:
                await CheckpointRepo(tx).save(
                    run_id, step_index=0, node_id=stage, status=status,
                    state={"stage": stage, "status": status, "reason": reason or None},
                    session_id=session_id, caller=caller,
                )
        except Exception:  # noqa: BLE001
            _log.exception(
                "检查点写入失败（运行 %s，状态 %s）。**续跑时这条运行会从头开始** —— "
                "不会重复产生副作用（那是幂等层的事），但会白花一次钱。",
                run_id, status,
            )

    # ---------------------------------------------------------------- 内部
    def _session_history(self, session_id: str) -> Sequence[Any]:
        if self.database is not None:
            # ⚠ ``repo/session.py`` / ``repo/message.py`` 目前是空的（`FR-A-07`）。
            # 落库一落地，这里就换成从 repo 读 —— 在那之前**必须让人知道**历史只在进程里。
            self._warn_no_persistence()
        else:
            self._warn_no_persistence()
        return self._history.get(session_id, ())

    def _warn_no_persistence(self) -> None:
        if self._warned_no_repo:
            return
        self._warned_no_repo = True
        _log.warning(
            "会话历史**只活在进程里**（repo 的 session/message 尚未实现）："
            "进程重启后 agent 会忘记之前聊过什么 —— 那看起来像「模型变笨了」。"
            "多轮任务请在同一进程内完成，或等会话落库落地。"
        )
        self._emit("degraded", {"phase": "history", "reason": "会话历史不落库（repo 未实现）"})

    def _trace_id(self) -> str:
        trace_id = current_trace_id()
        return "" if trace_id == "-" else trace_id

    def _emit(self, key: str, payload: Mapping[str, Any]) -> None:
        if not self.policy.emit_events:
            return
        name = DEFAULT_EVENTS.get(key, f"agent.{key}")
        try:
            (self.events or NullEventSink()).emit(name, payload)
        except Exception as exc:  # noqa: BLE001 — 事件出口不该影响主流程
            _log.debug("agent 事件发送失败：%s", exc)


def _exchange(user_input: str, answer: str) -> list[ContextMessage]:
    """把一轮对话变成两条领域消息。

    ⚠ **这是临时借用**：会话消息的领域形态本该由 ``src/runtime/message.py`` 定义，
    但那个模块还是空桩，所以这里借了上下文层的 ``ContextMessage``。
    两者形状接近但**不是一回事** —— 前者是「说过什么」，后者是「这次给模型看什么」。
    ``src/runtime`` 落地时这里应当换掉（`FR-A-07`）。
    """
    return [
        ContextMessage(role="user", content=user_input),
        ContextMessage(role="assistant", content=answer),
    ]

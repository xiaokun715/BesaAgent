"""一次阶段运行的执行循环 —— **想 → 调工具 → 再想**。

```
拼提示词 → 问模型 ──┬─ 模型要求调工具 → 执行 → 把结果回填 → 再问
                    └─ 模型给了答案   → 收尾（写检查点）
```

## ⚠ 检查点不负责「防止重复执行」

原设计写着「检查点与事件、用量同事务」—— 实现时发现**那句话解决不了它声称要解决的问题**：

    工具执行跨越事务边界（副作用在文件系统 / 子进程 / 外部 API 上），
    所以根本不存在「一个事务同时盖上工具执行与检查点」这回事。

真正防止续跑重复执行的是**幂等层**：同一个调用因为幂等键相同而被**复用**。
而那个机制**比检查点更可靠** —— 检查点整个丢了，重复执行也仍然被挡住。

所以两者分工是：

=================================  ============================================
**检查点**（``repo/checkpoint.py``）  「**从哪接着跑**」—— 少做无用功
**幂等**（``src/tool``）             「**不重复做**」—— 保正确性
=================================  ============================================

检查点丢了最多是从头再想一遍（多花点钱），而幂等丢了会重复产生副作用。
**把这两件事分开，才好在其中一个失效时知道自己在哪一档上。**
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from gateway.messages import Message

from foundation.clock import Clock, SystemClock
from foundation.database import Database
from foundation.logging import current_trace_id
from gateway.gateway import Gateway
from gateway.messages import (
    assistant_for_calls,
    system,
    tool_result,
    user,
)
from gateway.tools import to_tool_specs, tool_calls_of
from repo.checkpoint import CheckpointRepo
from tool.catalog import Catalog
from tool.executor import ToolExecutor
from tool.types import ToolInvocation

from agent.base import AgentResult, StageAgent
from agent.state import TaskRun

_log = logging.getLogger(__name__)

__all__ = ["AgentExecutor", "PrepareHook", "ProgressHook"]

#: 调模型**之前**的准备钩子：``(messages, step, remaining_s) -> messages``。
#:
#: **为什么是注入而不是让 executor import context**：executor 是「一次循环」，
#: 它不该认识「运行内上下文」这种编排层的东西（`MA-3`）。注入之后：
#:
#: - executor 保持纯循环，`prepare=None` 时**行为与以前逐字一致**（加法改动）；
#: - 上下文逻辑全部留在运行时层，可单独测；
#: - 换一种压缩策略不需要动循环。
#:
#: ⚠ **钩子不得切断工具调用与结果的配对** —— 换掉的是消息列表，
#: 但 `assistant(tool_calls)` 与 `tool(...)` 必须同进同出。
PrepareHook = Callable[[Sequence[Message], int, "float | None"], Awaitable[Sequence[Message]]]

#: 每完成一步就回报一次进度：``(这一步的花费, 累计工具调用次数) -> None``。
#:
#: **为什么要有它**：成本与工具次数这两个上界**只有运行时层知道怎么算**
#: （一个是"整轮够不够"、一个是顶层的兜底），而计数只有循环内做得到。
#: 少了这个回报，那两个上界就永远不会触发 —— 而"永远不会触发"看起来与
#: "从来没超支过"一模一样。``cost`` 为 ``None`` 表示这一步的价格未知（不是 0）。
ProgressHook = Callable[["Decimal | None", int], None]


@dataclass
class AgentExecutor:
    """把 gateway、工具层与检查点接成一个循环。

    **它持有的是「一次运行」需要的东西**，而不是某一阶段的逻辑 ——
    阶段之间的差别全在 :class:`~agent.base.StageAgent` 里（提示词 + 工具类别）。
    """

    gateway: Gateway
    tools: ToolExecutor
    catalog: Catalog
    database: Database | None = None
    #: 对话用的逻辑名。**阶段不该各配一个** —— 换模型是改配置的事
    alias: str = "runtime.default"
    #: 一次运行最多想几步。**给模型一个上界** —— 与工具层的
    #: ``max_calls_per_run`` 是同一个思路：给不可控的增长加一个可读的数字
    max_steps: int = 20

    #: 墙钟来源。**可注入**，好让「耗时」在测试里可复现（`NFR-A-06`）。
    clock: Clock | None = None

    # ⚠ **每次运行的钩子（prepare / remaining_s / stop_reason / call_timeout_s /
    # on_progress）全部是 ``run()`` 的参数，不是本对象的字段。**
    #
    # 原因是**并发**：``AgentExecutor`` 是长生命周期对象（组合根建一次、处处复用），
    # 而 multiagent 会**并发**跑好几个单 agent。若把钩子挂在实例上，
    # 两个并发运行会互相覆盖对方的准备钩子与预算 —— 表现是「A 的运行用了 B 的预算」，
    # 而它会**偶尔**发生、极难复现。参数是每次调用一份，天然没有这个问题。

    # ---------------------------------------------------------------- 入口
    async def run(
        self,
        agent: StageAgent,
        task: str,
        *,
        run_id: str = "",
        session_id: str | None = None,
        caller: str | None = None,
        resume: bool = False,
        messages: Sequence[Message] | None = None,
        prepare: PrepareHook | None = None,
        remaining_s: Callable[[], float | None] | None = None,
        stop_reason: Callable[[], str] | None = None,
        call_timeout_s: float | None = None,
        on_progress: ProgressHook | None = None,
    ) -> AgentResult:
        """跑一个阶段。

        Args:
            messages: **已经准备好的消息序列**。运行时层（它会用上下文层拼历史与预算）
                传它进来；``None`` 时退回本模块自己的最小拼法（``system + user``）——
                保持今天的行为，方便单独测这个循环。
            resume: 为真时**先找这个 ``run_id`` 的最近快照**，把「已经做到哪了」
                写进提示词。**找不到快照不是错误** —— 那说明上一次什么都没做成，
                从头跑就是对的。但**版本不认识会抛**（见 ``repo/checkpoint``）。
        """
        run_id = run_id or uuid.uuid4().hex
        trace_id = current_trace_id()
        if trace_id == "-":
            trace_id = ""

        resumed = await self._load(run_id) if resume else None
        run = resumed or TaskRun(
            run_id=run_id, stage=agent.stage, goal=task, session_id=session_id, caller=caller
        )
        if resumed is not None:
            run.goal = task
            _log.info(
                "续跑 %s：从第 %d 步接着做（已完成 %d 步）",
                run_id,
                run.next_index + 1,
                run.next_index,
            )

        if messages is None:
            prepared = [
                system(agent.system_prompt),
                user(agent.build_prompt(task, resumed=resumed)),
            ]
        else:
            prepared = list(messages)
        messages = prepared
        tool_calls = 0
        clock = self.clock or SystemClock()
        started = clock.monotonic()
        spent = Decimal("0")
        cost_known = True

        def _elapsed() -> float:
            return max(0.0, clock.monotonic() - started)

        def _cost() -> Decimal | None:
            # **未知不当 0**（`FR-G-09`）：只要有一次价格未知，合计就是未知 ——
            # 把未知抹成 0 会让成本上界永远不触发。
            return spent if cost_known else None

        try:
            for step in range(self.max_steps):
                # 上界检查：**撞上哪个要说清是哪个**（`FR-A-05`），
                # 否则排障时只知道「停了」，不知道是钱、是次数、还是时间。
                stopped = stop_reason() if stop_reason is not None else ""
                if stopped:
                    run.finish("failed")
                    await self._save(run, status="failed")
                    return AgentResult(
                        run_id=run_id, stage=agent.stage, status="failed",
                        steps=len(run.steps), tool_calls=tool_calls,
                        error=stopped, cost=_cost(), elapsed_s=_elapsed(),
                    )

                remaining = remaining_s() if remaining_s is not None else None
                if remaining is not None and remaining <= 0:
                    # **时间预算耗尽是一个明确的失败**，不是一个可以忽略的状态。
                    run.finish("failed")
                    await self._save(run, status="failed")
                    return AgentResult(
                        run_id=run_id,
                        stage=agent.stage,
                        status="failed",
                        steps=len(run.steps),
                        tool_calls=tool_calls,
                        error=(
                            f"达到时间预算，在第 {step + 1} 步停止。"
                            "要跑更久请调大 agent.limits.deadline_s —— "
                            "但先看看它是不是在原地打转。"
                        ),
                        cost=_cost(),
                        elapsed_s=_elapsed(),
                    )

                if prepare is not None:
                    # 运行内压缩的接入点：**在调模型之前**，让运行时层决定
                    # "这一轮给模型看什么"。钩子失败**不阻断运行** —— 用原样的
                    # 消息继续（压缩是优化，不是依赖）。
                    try:
                        messages = list(await prepare(messages, step, remaining))
                    except Exception:  # noqa: BLE001
                        _log.exception(
                            "上下文准备钩子失败（第 %d 步）—— 本次用原样的消息继续。"
                            "**这次可能超出模型窗口**，但比整轮失败好。",
                            step + 1,
                        )

                specs = await self._specs(agent, session_id=session_id)
                result = await self.gateway.chat(
                    self.alias,
                    messages,
                    tools=specs or None,
                    session_id=session_id,
                    caller=caller,
                    trace_id=trace_id,
                    deadline_s=_pick_deadline(remaining, call_timeout_s),
                )

                amount = getattr(getattr(result, "cost", None), "amount", None)
                delta = amount
                if amount is None:
                    cost_known = False
                else:
                    spent += amount

                calls = tool_calls_of(result)
                if not calls:
                    # 模型不再要工具了 —— 它给了答案
                    run.finish("done")
                    await self._save(run, status="done")
                    return AgentResult(
                        run_id=run_id,
                        stage=agent.stage,
                        status="done",
                        answer=result.content,
                        steps=len(run.steps),
                        tool_calls=tool_calls,
                        cost=_cost(),
                        elapsed_s=_elapsed(),
                    )

                # **先把「模型的工具调用」记进对话**，再执行并回填结果 ——
                # 顺序反过来的话，工具结果会成为一条没有前因的消息，
                # 而多数厂商会直接 400（报「消息顺序不对」）。
                messages.append(assistant_for_calls(calls))
                used: list[str] = []
                # **记下幂等键**（`AQ-2`）：续跑时要拿它去查 `tool_execution`，
                # 才能回答「上一次那个调用到底执行完了没有」。只记工具名复现不出要查哪一行。
                idem_keys: list[str] = []
                for call in calls:
                    outcome = await self.tools.invoke(
                        ToolInvocation(
                            tool_name=call.name,
                            arguments=call.arguments,
                            # **作用域取 run_id**：同一次运行里，同一个工具 + 同一份参数
                            # 就是同一次调用（幂等据此去重）。用 session 会让
                            # 「同一会话里两次确实要各跑一遍的调用」被误判成重复。
                            scope=run_id,
                            trace_id=trace_id,
                            session_id=session_id,
                            caller=caller,
                        )
                    )
                    tool_calls += 1
                    used.append(call.name)
                    if outcome.idem_key:
                        idem_keys.append(outcome.idem_key)
                    messages.append(tool_result(call.id, outcome.to_model_text()))

                if on_progress is not None:
                    on_progress(delta, tool_calls)

                run.record(
                    node_id=agent.stage,
                    summary=f"调用 {', '.join(used)}",
                    tools_used=tuple(used),
                    idem_keys=tuple(idem_keys),
                )
                await self._save(run, status="running")

            # 步数用尽 —— **这是一个明确的失败，不是一个可以忽略的状态**
            run.finish("failed")
            await self._save(run, status="failed")
            return AgentResult(
                run_id=run_id,
                stage=agent.stage,
                status="failed",
                answer="",
                steps=len(run.steps),
                tool_calls=tool_calls,
                error=(
                    f"达到步数上界（{self.max_steps}）仍未收敛。"
                    "要调大请改 agent 的 max_steps —— 但先看看它是不是在原地打转。"
                ),
                cost=_cost(),
                elapsed_s=_elapsed(),
            )
        except Exception as exc:  # noqa: BLE001
            # **异常也要留快照**：不留的话，下次续跑只能从头来，
            # 而「上次是崩在哪一步」这件事就永远查不到了。
            run.finish("failed")
            await self._save(run, status="failed")
            _log.exception("阶段 %s 运行失败", agent.stage)
            return AgentResult(
                run_id=run_id,
                stage=agent.stage,
                status="failed",
                steps=len(run.steps),
                tool_calls=tool_calls,
                error=f"{type(exc).__name__}: {exc}",
                cost=_cost(),
                elapsed_s=_elapsed(),
            )

    # ---------------------------------------------------------------- 内部
    async def _specs(self, agent: StageAgent, *, session_id: str | None):
        """挑出这一轮该给模型看的工具。

        按**阶段类别**筛（`FR-T-13`）—— 一个阶段只该看见它那类工具 + ``infra``。
        给模型看见别的阶段的工具，它会去调用然后被拒。
        """
        definitions = await self.catalog.select(
            categories=agent.tool_categories(), session_id=session_id or ""
        )
        if agent.tools:
            allowed = set(agent.tools)
            definitions = tuple(d for d in definitions if d.name in allowed)
        return to_tool_specs(definitions)

    async def _load(self, run_id: str) -> TaskRun | None:
        if self.database is None:
            return None
        async with self.database.transaction() as tx:
            view = await CheckpointRepo(tx).latest(run_id)
        if view is None or view.finished:
            # **已完成的不续跑**：那不叫续跑，叫重跑一个已经做完的任务。
            return None
        return TaskRun.from_state(
            run_id, dict(view.state), session_id=None, caller=None
        )

    async def _save(self, run: TaskRun, *, status: str) -> None:
        """写一条快照。

        **失败只告警，不影响运行** —— 与「用量落库失败不拖垮调用」同一条纪律：
        留痕是旁路，不是依赖。但**必须出声**，否则「为什么续跑从头开始了」查不出来。
        """
        if self.database is None:
            return
        try:
            async with self.database.transaction() as tx:
                await CheckpointRepo(tx).save(
                    run.run_id,
                    step_index=run.next_index,
                    node_id=run.stage,
                    status=status,
                    state=run.to_state(),
                    session_id=run.session_id,
                    caller=run.caller,
                )
        except Exception:  # noqa: BLE001
            _log.exception(
                "检查点写入失败（运行 %s）。**续跑时这条运行会从头开始** —— "
                "不会重复产生副作用（那是幂等层的事），但会白花一次钱。",
                run.run_id,
            )


def _pick_deadline(remaining_s: float | None, call_timeout_s: float | None) -> float | None:
    """这次调用该给多久：**整轮剩余与单次上限取小的那个**。

    两个都要给（`MA-8`）：

    - 只给单次上限 → 「每次都不超时，合起来一小时」；
    - 只给整轮剩余 → 单次调用可能一口吃掉全部预算，剩下的步骤全都没得跑。
    """
    candidates = [value for value in (remaining_s, call_timeout_s) if value is not None]
    if not candidates:
        return None
    return min(candidates)

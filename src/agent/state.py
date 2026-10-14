"""一次运行的**状态**：跑到哪了、做过什么、产出了什么。

**它是「断点续跑」的载体**（`FR-R-05`），也是 agent 与工具的分界：

    工具报告「**这一次调用**」（``tool_execution`` + 事件）
    agent 报告「**这一步 / 这次运行**」（本模块 + ``repo/checkpoint``）

工具只拿到 ``scope``，它不知道自己在一个什么样的运行里 ——
所以检查点由 agent 写。这与「谁拥有这个信息，谁记录它」是同一条纪律。

**状态要**可序列化**且**存摘要不存全文**：它是状态不是日志。
全文在 ``tool_execution`` 与（大结果的）落盘文件里 —— 那些地方才是回看细节的去处。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

__all__ = [
    "RUN_STATUSES",
    "AgentStep",
    "RunStatus",
    "TaskRun",
]

#: 一次运行的状态。
#:
#: ``needs_review`` 是**独立状态**（`docs/agent` 的 `MA-7`），不塞进 ``failed``：
#: 它不是失败，是「**这里需要一个人看一眼，程序不替你赌**」。
#: 目前有两种成因：续跑时发现「工具执行过、结果不明」，以及路由需要调用方指定阶段。
RunStatus = Literal["running", "done", "failed", "cancelled", "needs_review"]

RUN_STATUSES: tuple[RunStatus, ...] = (
    "running", "done", "failed", "cancelled", "needs_review",
)


@dataclass(frozen=True)
class AgentStep:
    """一步。**存摘要不存全文** —— 见模块 docstring。"""

    index: int
    node_id: str = ""
    summary: str = ""
    #: 这一步用了哪些工具（只有名字，细节在 tool_execution 里）
    tools_used: tuple[str, ...] = ()
    #: 这一步用到的**幂等键**（`(scope, idem_key)` 里的那个 key）。
    #:
    #: **为什么必须记**（`docs/agent` 的 `AQ-2`）：续跑时要做一次判定 ——
    #: 「上一次停下的那一步，工具到底执行完了没有？」而那个判定要拿
    #: **(scope, 幂等键)** 去查 `tool_execution`（工具层的 `find` 会按租约把超时的
    #: `in_flight` 判成 `uncertain`）。
    #:
    #: 只记工具**名**是不够的：幂等键含参数指纹，同一个工具用不同参数调两次
    #: 是两行记录 —— 复现不出要查哪一行，判定就只能退化成「整步都算不确定」。
    idem_keys: tuple[str, ...] = ()
    created_at: datetime | None = None

    def to_state(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "node_id": self.node_id,
            "summary": self.summary,
            "tools": list(self.tools_used),
            "idem_keys": list(self.idem_keys),
        }


@dataclass
class TaskRun:
    """一次任务运行的状态。

    **它是可变的**（与项目里那些 frozen 的类型不同）—— 因为它的用途就是
    「随着执行不断推进」。要交给外面时用 :meth:`to_state` 取一份**快照**，
    而不是把可变对象递出去。
    """

    run_id: str
    stage: str
    goal: str = ""
    status: RunStatus = "running"
    session_id: str | None = None
    caller: str | None = None
    steps: list[AgentStep] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    #: 续跑时记下「从哪一步接着跑」—— 它是恢复的起点，不是结果
    resumed_from: int | None = None

    @property
    def next_index(self) -> int:
        return len(self.steps)

    def record(
        self,
        *,
        node_id: str = "",
        summary: str = "",
        tools_used: tuple[str, ...] = (),
        idem_keys: tuple[str, ...] = (),
    ) -> AgentStep:
        """记一步。**索引由这里分配**（唯一计数点的同一个思路）。"""
        step = AgentStep(
            index=self.next_index,
            node_id=node_id,
            summary=summary,
            tools_used=tuple(tools_used),
            idem_keys=tuple(idem_keys),
            created_at=datetime.now(timezone.utc),
        )
        self.steps.append(step)
        return step

    def finish(self, status: RunStatus) -> None:
        self.status = status

    def to_state(self) -> dict[str, Any]:
        """给 ``repo/checkpoint`` 的快照。

        **带上 ``resumed_from``**：一份从第 3 步恢复的运行，和一份从头跑的运行，
        即使当前状态一样，含义也完全不同 —— 排障时那个数字是必需的。
        """
        return {
            "stage": self.stage,
            "goal": self.goal,
            "status": self.status,
            "steps": [step.to_state() for step in self.steps],
            "artifacts": dict(self.artifacts),
            "resumed_from": self.resumed_from,
        }

    @classmethod
    def from_state(
        cls,
        run_id: str,
        state: dict[str, Any],
        *,
        session_id: str | None = None,
        caller: str | None = None,
    ) -> TaskRun:
        """从快照恢复。

        ⚠ **格式版本的校验在 ``repo/checkpoint`` 那一层做**（见那里的
        ``UnknownCheckpointVersion``）—— 本方法只负责把「已经确认版本对得上」的
        状态还原成对象。两处都判会让「谁该拒绝」变得模糊。
        """
        steps = [
            AgentStep(
                index=int(item.get("index", index)),
                node_id=str(item.get("node_id", "")),
                summary=str(item.get("summary", "")),
                tools_used=tuple(item.get("tools") or ()),
                idem_keys=tuple(item.get("idem_keys") or ()),
            )
            for index, item in enumerate(state.get("steps") or ())
        ]
        return cls(
            run_id=run_id,
            stage=str(state.get("stage", "")),
            goal=str(state.get("goal", "")),
            status="running",  # 恢复出来的运行**一定**是 running：上一轮没跑完
            session_id=session_id,
            caller=caller,
            steps=steps,
            artifacts=dict(state.get("artifacts") or {}),
            resumed_from=len(steps),
        )

    def __repr__(self) -> str:
        return (
            f"TaskRun(run={self.run_id!r}, stage={self.stage!r}, "
            f"status={self.status!r}, steps={len(self.steps)})"
        )

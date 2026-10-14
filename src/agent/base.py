"""阶段智能体的契约。

**七个测试阶段**（需求 → 测试设计 → 用例 → 环境 → 执行 → 分析 → 缺陷）
各自是一个 ``StageAgent``。它们的**执行流程完全相同**（都是「想 → 调工具 → 再想」），
差别只在**提示词**与**能用哪些工具**。

所以契约很薄，而 :class:`SimpleStageAgent` 覆盖了绝大多数阶段 ——
**不要为七个阶段各写一套一模一样的循环**，那是「同一件事的七个副本」，
而项目里已经吃过那个亏（gateway 的顺序规则有两个实现）。

真有阶段需要不一样的流程时（例如「执行」阶段要并发跑用例、「缺陷」阶段要回写外部系统），
再让它实现自己的 :meth:`~StageAgent.build_prompt` 或完全自己跑循环。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from agent.state import RunStatus, TaskRun

__all__ = ["AgentResult", "SimpleStageAgent", "StageAgent"]


@dataclass(frozen=True)
class AgentResult:
    """一次阶段运行的产出。

    ``steps`` 与 ``tool_calls`` 分开计：**「想了 5 步」与「调了 5 次工具」
    是完全不同的两件事** —— 前者说明任务复杂，后者说明它一直在做动作。
    合并成一个「轮数」会让这两类问题分不开。
    """

    run_id: str
    stage: str
    status: RunStatus
    answer: str = ""
    steps: int = 0
    tool_calls: int = 0
    error: str = ""
    #: 这一轮的花费。**``None`` 表示"价格未知"，不是 0 元**（`FR-G-09`）。
    #: 只要有一次调用的价格未知，合计就是未知 —— 把未知当 0 会让成本上界形同虚设。
    cost: Decimal | None = None
    #: 墙钟耗时（秒）
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def __str__(self) -> str:
        mark = "✓" if self.ok else "✗"
        detail = self.error or f"{len(self.answer)} 字符"
        return f"{mark} {self.stage} [{self.status}] {self.steps} 步 / {self.tool_calls} 次工具：{detail}"


class StageAgent(ABC):
    """一个测试阶段的智能体。"""

    #: 阶段标识。**同时用作工具类别**（``FR-T-13``）：
    #: 一个阶段只该看见它那类工具 + ``infra``。
    stage: ClassVar[str] = ""
    #: 系统提示词
    system_prompt: ClassVar[str] = ""
    #: 这个阶段**打算**用哪些工具。``()`` = 不限（用类别筛）。
    #:
    #: 注意它只是**意图**，真正的授权在 ``src/tool`` 的权限层 ——
    #: 这里声明得再宽，没授权也调不动。反过来（声明得窄但授权宽）才是危险的，
    #: 所以这个列表应当**宁窄勿宽**。
    tools: ClassVar[tuple[str, ...]] = ()

    #: 供**规则路由**打分用的关键词（`FR-A-02`）。
    #:
    #: 放在阶段上而不是路由里：它是**阶段的属性**（"什么话看起来像这个阶段的活"），
    #: 与提示词一样属于阶段定义 —— 新增阶段时应当一并给出。
    keywords: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def build_prompt(self, task: str, *, resumed: TaskRun | None = None) -> str:
        """把任务（与续跑状态）拼成给模型的用户消息。

        ``resumed`` 非空表示这是**断点续跑** —— 提示词必须把「已经做到哪了」
        说清楚，否则模型会从头再做一遍，而那正是检查点要避免的事。
        """
        ...

    def tool_categories(self) -> tuple[str, ...]:
        """给模型看哪些类别的工具。**默认是本阶段 + ``infra``**。

        ``infra``（读文件、跑命令这类通用工具）几乎每个阶段都要用，
        所以它总在里面。业务类别的工具只暴露本阶段那一类 ——
        给模型看见别的阶段的工具，它会去调用然后被拒，白费一轮往返。
        """
        return (self.stage, "infra")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(stage={self.stage!r})"


@dataclass
class SimpleStageAgent(StageAgent):
    """数据驱动的阶段 agent —— 七个阶段里大多数只是提示词不同。

    用组合而不是继承：新增一个阶段 = 写一段提示词，
    而不是复制一个类和它的循环。
    """

    stage: str = ""
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    #: 供 ``doctor`` / 路由展示的一句话
    description: str = ""

    def __post_init__(self) -> None:
        if not self.stage.strip():
            raise ValueError("阶段 agent 必须有 stage")
        if not self.system_prompt.strip():
            raise ValueError(f"阶段 {self.stage!r} 没有系统提示词 —— 那它就没有自己的立场")

    def build_prompt(self, task: str, *, resumed: TaskRun | None = None) -> str:
        if resumed is None:
            return task

        # **续跑时必须把「已经做到哪了」写进提示词**。不写的话模型会从头再做一遍 ——
        # 而「不重复已完成的步骤」正是检查点存在的全部理由。
        done = "\n".join(
            f"  {step.index + 1}. {step.summary or '（无摘要）'}" for step in resumed.steps
        ) or "  （一步都没完成）"
        return (
            f"这是一个**续跑**的任务：上一次运行在第 {resumed.next_index} 步中断了。\n"
            f"已经完成的步骤：\n{done}\n\n"
            f"**不要重做上面这些步骤**，从第 {resumed.next_index + 1} 步接着做。\n\n"
            f"任务原文：{task}"
        )

"""七个阶段是**数据**，不是七个类（`FR-A-01` / `MA-9`）。

**为什么不写七个类**（`base.py` 的 docstring 已经说过一次，这里从注册表的角度再说一遍）：
七个阶段的**执行流程完全相同**（都是「想 → 调工具 → 再想」），差别只在**提示词**
与**能用哪些工具**。为它们各写一个类，就是同一件事的七个副本 ——
而项目已经吃过那个亏（gateway 的顺序规则曾有两个实现）。

所以这里做的是：

```
agent/<stage>/prompt.py   内置提示词（纯数据）
        +
configs: agent.stages.*   启用 / 描述 / 工具 / 提示词覆盖
        ↓
   StageRegistry ──→ SimpleStageAgent（base.py 里那个数据驱动的实现）
```

**新增一个阶段 = 加一个包 + 一条配置**，不改 `runtime` / `router` / `executor` 一行代码。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent.analysis.prompt import KEYWORDS as _ANALYSIS_KW
from agent.analysis.prompt import SYSTEM_PROMPT as _ANALYSIS
from agent.base import StageAgent
from agent.base import SimpleStageAgent
from agent.defect.prompt import KEYWORDS as _DEFECT_KW
from agent.defect.prompt import SYSTEM_PROMPT as _DEFECT
from agent.environment.prompt import KEYWORDS as _ENV_KW
from agent.environment.prompt import SYSTEM_PROMPT as _ENV
from agent.execution.prompt import KEYWORDS as _EXEC_KW
from agent.execution.prompt import SYSTEM_PROMPT as _EXEC
from agent.requirement.prompt import KEYWORDS as _REQ_KW
from agent.requirement.prompt import SYSTEM_PROMPT as _REQ
from agent.test_case.prompt import KEYWORDS as _CASE_KW
from agent.test_case.prompt import SYSTEM_PROMPT as _CASE
from agent.test_design.prompt import KEYWORDS as _DESIGN_KW
from agent.test_design.prompt import SYSTEM_PROMPT as _DESIGN

__all__ = [
    "BUILTIN_STAGES",
    "STAGE_ORDER",
    "StageDefinition",
    "StageRegistry",
    "UnknownStageError",
    "build_stages",
]

_log = logging.getLogger(__name__)

#: 七个阶段的**固定顺序** —— 它就是测试流程本身（需求 → 设计 → 用例 → 环境 → 执行 → 分析 → 缺陷）。
#: 顺序是契约的一部分：`doctor` 按它展示，未来的编排按它推下一步。
STAGE_ORDER: tuple[str, ...] = (
    "requirement",
    "test_design",
    "test_case",
    "environment",
    "execution",
    "analysis",
    "defect",
)


@dataclass(frozen=True)
class StageDefinition:
    """一个阶段的定义。**纯数据** —— 它决定 agent 的立场与能用的工具。"""

    stage: str
    description: str = ""
    prompt: str = ""
    #: 路由的规则层用。**宁少勿滥**：太泛的词会把别的阶段也捞进来
    keywords: tuple[str, ...] = ()
    #: 这个阶段**打算**用哪些工具。``()`` = 用 ``tool_categories()``（本阶段 + infra）
    tools: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.stage.strip():
            raise ValueError("阶段定义必须有 stage")
        if self.enabled and not self.prompt.strip():
            # 没有提示词 = 没有立场。这与 ``SimpleStageAgent.__post_init__`` 是同一条纪律，
            # 在这里再拦一次是为了让错误在**配置加载期**就暴露，而不是等到构造 agent 时。
            raise ValueError(f"阶段 {self.stage!r} 没有提示词 —— 那它就没有自己的立场")


#: 七个内置阶段。**顺序即流程**。
BUILTIN_STAGES: tuple[StageDefinition, ...] = (
    StageDefinition("requirement", "需求分析", _REQ, _REQ_KW),
    StageDefinition("test_design", "测试设计", _DESIGN, _DESIGN_KW),
    StageDefinition("test_case", "用例编写", _CASE, _CASE_KW),
    StageDefinition("environment", "环境准备", _ENV, _ENV_KW),
    StageDefinition("execution", "测试执行", _EXEC, _EXEC_KW),
    StageDefinition("analysis", "结果分析", _ANALYSIS, _ANALYSIS_KW),
    StageDefinition("defect", "缺陷管理", _DEFECT, _DEFECT_KW),
)


class UnknownStageError(ValueError):
    """未知的阶段名。

    **错误信息里列出全部可用阶段** —— 与 ``FR-G-02`` 的逻辑名寻址同一条纪律：
    拼错是最常见的错误，只说「未知」会让人去翻文档。
    """

    def __init__(self, stage: str, known: tuple[str, ...]) -> None:
        self.stage = stage
        self.known = known
        super().__init__(
            f"未知的阶段 {stage!r}；已注册：{', '.join(known) or '（无）'}"
        )


class StageRegistry:
    """阶段注册表。**重名硬失败**（`FR-T-02` 的同一纪律）。"""

    __slots__ = ("_stages",)

    def __init__(self) -> None:
        self._stages: dict[str, StageAgent] = {}

    def register(self, agent: StageAgent) -> None:
        if not agent.stage.strip():
            raise ValueError("阶段 agent 必须有 stage")
        if agent.stage in self._stages:
            # 静默覆盖会让一个阶段悄悄失效 —— 而表现是"某些任务突然换了立场"，
            # 离原因很远。
            raise ValueError(
                f"阶段 {agent.stage!r} 重复注册 —— 覆盖会让其中一个悄悄失效，所以这里硬失败"
            )
        self._stages[agent.stage] = agent

    def get(self, stage: str) -> StageAgent:
        agent = self._stages.get(stage)
        if agent is None:
            raise UnknownStageError(stage, self.names())
        return agent

    def has(self, stage: str) -> bool:
        return stage in self._stages

    def names(self) -> tuple[str, ...]:
        """按**流程顺序**给出已注册的阶段（顺序外的排后面，按名字）。"""
        ordered = [name for name in STAGE_ORDER if name in self._stages]
        ordered += sorted(set(self._stages) - set(ordered))
        return tuple(ordered)

    def all(self) -> tuple[StageAgent, ...]:
        return tuple(self._stages[name] for name in self.names())

    def __len__(self) -> int:
        return len(self._stages)

    def __contains__(self, stage: object) -> bool:
        return stage in self._stages


def build_stages(
    cfg: Mapping[str, Any] | None,
    *,
    known_tools: frozenset[str] | None = None,
) -> StageRegistry:
    """按配置装配七个阶段。

    Args:
        cfg: ``agent.stages`` 段。**缺省 = 七个内置阶段全开**。
        known_tools: 工具注册表里的名字。给了就校验阶段声明的工具名 ——
            不一致时**告警**（`FR-A-03` / `C-A-5`）：配置错误要在启动时暴露，
            而不是等模型调到一个不存在的工具。
    """
    data = dict(cfg or {})
    registry = StageRegistry()

    definitions: list[StageDefinition] = []
    for builtin in BUILTIN_STAGES:
        override = data.get(builtin.stage)
        override = override if isinstance(override, Mapping) else {}
        if not bool(override.get("enabled", True)):
            continue
        definitions.append(
            StageDefinition(
                stage=builtin.stage,
                description=str(override.get("description") or builtin.description),
                prompt=str(override.get("prompt") or builtin.prompt),
                keywords=tuple(str(x) for x in (override.get("keywords") or builtin.keywords)),
                tools=tuple(str(x) for x in (override.get("tools") or builtin.tools)),
                enabled=True,
            )
        )

    # 配置里出现的内置之外的阶段 —— **也允许**（数据驱动：加阶段不该改代码）
    for name, override in data.items():
        if name in STAGE_ORDER or not isinstance(override, Mapping):
            continue
        if not bool(override.get("enabled", True)):
            continue
        definitions.append(
            StageDefinition(
                stage=str(name),
                description=str(override.get("description") or name),
                prompt=str(override.get("prompt") or ""),
                keywords=tuple(str(x) for x in (override.get("keywords") or ())),
                tools=tuple(str(x) for x in (override.get("tools") or ())),
                enabled=True,
            )
        )

    for definition in definitions:
        if known_tools is not None:
            unknown = [name for name in definition.tools if name not in known_tools]
            if unknown:
                _log.warning(
                    "阶段 %r 声明了未注册的工具：%s。它们会被静默过滤掉，"
                    "模型因此看不到它们 —— 请检查配置里的工具名。",
                    definition.stage, ", ".join(unknown),
                )
        registry.register(
            SimpleStageAgent(
                stage=definition.stage,
                system_prompt=definition.prompt,
                tools=definition.tools,
                keywords=definition.keywords,
                description=definition.description,
            )
        )
    return registry

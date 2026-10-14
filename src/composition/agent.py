"""把单 agent 的运行时装配起来。

**这个文件存在，是因为几条依赖方向在这里交叉**（与 ``composition/tools.py`` 同构）：

- ``src/agent`` **不能** import ``provider`` —— 所以「一次补全」由这里适配
  （复用 :class:`composition.memory.GatewayChat`，把 ``Gateway.chat`` 包成
  ``ChatModel`` 协议给路由用）；
- ``src/agent`` **不能** import ``apps`` —— 数据库由 app 层注入；
- 阶段声明的工具名要对着**工具注册表**校验（`FR-A-03`），而注册表是这里建的。

**上下文是可选的**：``context.enabled=false`` 时 ``context=None``，
运行时退回 executor 的最小拼法 —— 那是一條完整路径，只是运行内的压缩没有了。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from composition.memory import GatewayChat

from agent.executor import AgentExecutor
from agent.router import StageRouter
from agent.runtime import AgentPolicy, AgentRuntime
from agent.stages import build_stages

__all__ = ["AgentBundle", "build_agent"]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentBundle:
    """装配好的 agent 运行时。"""

    runtime: AgentRuntime
    policy: AgentPolicy


def build_agent(
    *,
    agent_cfg: Mapping[str, Any] | None,
    gateway: Any,
    tools: Any,
    catalog: Any,
    context: Any | None = None,
    database: Any | None = None,
    events: Any | None = None,
    clock: Any | None = None,
) -> AgentBundle | None:
    """按 ``agent`` 段装配。``agent.enabled=false`` 时返回 ``None``。

    Args:
        tools: **工具层的**执行器（``ToolExecutor``）—— 注意与
            :class:`agent.executor.AgentExecutor` 不是一回事：
            前者执行**一次工具调用**，后者跑**一次 agent 循环**。
            两者都在这里被接起来。
        catalog: 工具目录（按阶段类别筛工具）。
    """
    cfg = dict(agent_cfg or {})
    if not bool(cfg.get("enabled", True)):
        _log.info("单 agent 运行时未启用（agent.enabled=false）")
        return None

    policy = AgentPolicy.from_config(cfg)

    # ① **agent 自己的执行循环** —— 与工具层的执行器是两回事。
    executor = AgentExecutor(
        gateway=gateway,
        tools=tools,
        catalog=catalog,
        database=database,
        alias=policy.alias,
        max_steps=policy.limits.max_steps,
        clock=clock,
    )

    # ② 七个阶段（数据驱动）。拿工具注册表校验阶段声明的工具名 ——
    #    配置错误在**启动期**暴露，而不是等模型调到一个不存在的工具。
    known_tools = _tool_names(tools)
    stages = build_stages(cfg.get("stages"), known_tools=known_tools)

    # ③ 路由。LLM 判断是可选的：关掉它照样工作（退到 on_unknown 策略）。
    routing = _mapping(cfg.get("routing"))
    raw_policy = routing.get("on_unknown")
    if raw_policy is None:
        # ⚠ **这一条在设计时写的是"没有默认值、必须显式配"，实现时改掉了**（见架构文档 §14）。
        #
        # 理由是它与 ``idempotency.on_unavailable`` 那种"必须显式配"**不是一类**：
        # 那一条选错了会**静默地不设防**；而这里的三个选项**都是安全的**，
        # 差别只在体验（问一句 / 兜底跑 / 直接拒绝）。为一个不影响安全的取舍
        # 让最小配置起不来，代价与收益不成比例。
        #
        # 所以：默认取**最保守的那个**（`ask` —— 不猜、不乱跑），**并告警**让人知道。
        raw_policy = "ask"
        _log.warning(
            "agent.routing.on_unknown 未配置，按最保守的 ask 处理："
            "任务推断不出阶段时会返回「需要指定阶段」。"
            "批处理场景建议显式配成 reject；想兜底跑请配 default 并给 default_stage。"
        )
    on_unknown = str(raw_policy)
    router = StageRouter(
        stages,
        on_unknown=on_unknown,  # type: ignore[arg-type]
        default_stage=str(routing.get("default_stage") or ""),
        use_llm=bool(routing.get("use_llm", False)),
        chat=GatewayChat(gateway, str(routing.get("llm_alias") or policy.alias))
        if bool(routing.get("use_llm", False))
        else None,
        rule_threshold=float(routing.get("rule_threshold", 0.5)),
    )

    runtime = AgentRuntime(
        executor=executor,
        stages=stages,
        router=router,
        context=context,
        database=database,
        policy=policy,
        clock=clock,
        events=events,
    )

    _log.info(
        "单 agent 运行时装配完成：阶段 %d 个（%s），路由未知时=%s，"
        "上限 步数=%d/时间=%s/成本=%s，运行内上下文=%s",
        len(stages),
        ", ".join(stages.names()) or "（无）",
        on_unknown,
        policy.limits.max_steps,
        f"{policy.limits.deadline_s:.0f}s" if policy.limits.deadline_s else "不限",
        policy.limits.max_cost if policy.limits.max_cost is not None else "不限",
        "开" if (context is not None and policy.context.enabled) else "关",
    )
    return AgentBundle(runtime=runtime, policy=policy)


def _tool_names(tools: Any) -> frozenset[str] | None:
    """工具注册表里的名字。取不到就返回 ``None``（**跳过校验，而不是假装校验过**）。"""
    registry = getattr(tools, "registry", None)
    names = getattr(registry, "names", None)
    if names is None:
        return None
    try:
        return frozenset(names)
    except TypeError:
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}

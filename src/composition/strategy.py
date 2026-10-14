"""把组件策略层装配起来：**登记声明 → 建契约 → 建模板 → 建选择器**。

**这个文件是"声明与实现的分界"落地的地方**（架构文档 §6）：

    各组件模块声明自己的策略  →  本层只认声明  →  组合根持有工厂

工厂**不在** ``src/strategy`` 里（`MST-3`）—— 只要那里有了工厂，就会有人
忍不住在 ``selector`` 里"顺手启一个组件试试"，那一刻这一层就开始执行了。

## 一期登记什么（`STQ-1`）

**只登记真有实现的**，其余登记为 ``unavailable`` 并挂上原因 ——
与 ``ModelSpec.unavailable_reason`` 同一条纪律：**保留在册，但不假装可用**。
"为什么不能用它"是排障时最常见的问题之一，不登记会让它变成一个查不到的空缺。

⚠ 声明里的 ``cost`` 与 ``metrics`` 是**量级估计**，不是实测值（架构文档 §2.2）。
它们允许不准 —— 不准会被以后的评测数据纠正回来；不填则永远不会被纠正。

## 与 `AgentRuntime` 的关系

本模块**不碰** agent：它产出 ``AgentProfile``（一份配置），
而真的拿它去跑任务、以及把 profile 变成组件行为，是后续的事（见 §12 的说明）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from strategy import (
    CostEstimate,
    EvaluatedSource,
    EvalRecord,
    SelectionPolicy,
    StrategyRegistry,
    StrategySelector,
    StrategySpec,
    default_contracts,
    build_templates,
)

__all__ = ["StrategyBundle", "build_strategy", "builtin_specs", "describe"]

_log = logging.getLogger(__name__)


class _NoEvaluatedData:
    """还没有评测数据时的来源。

    **返回空而不是报错**：选择器遇到"没有数据"会走降级 + 告警（`FR-ST-10` 的最后一档），
    那是**正确行为**；抛异常反而会让"还没跑过评测"变成一次启动失败。
    """

    __slots__ = ()

    def records(self) -> Sequence[EvalRecord]:
        return ()


@dataclass(frozen=True)
class StrategyBundle:
    """装配好的组件策略层。"""

    selector: StrategySelector
    registry: StrategyRegistry
    policy: SelectionPolicy
    issues: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 一期登记的声明（`STQ-1`：只登记真有实现的）
# --------------------------------------------------------------------------- #


def _spec(
    kind: str,
    name: str,
    *,
    version: str = "1",
    interface: str,
    provides: Sequence[str] = (),
    inputs: Sequence[str] = (),
    preconditions: Sequence[str] = (),
    tokens: int = 0,
    latency_ms: int = 0,
    external_calls: int = 0,
    scenarios: Sequence[str] = (),
    metrics: Mapping[str, str] | None = None,
    available: bool = True,
    unavailable_reason: str = "",
) -> StrategySpec:
    """构造一条声明。**六项由调用点显式给**（缺了 StrategySpec 会硬失败）。"""
    return StrategySpec(
        kind=kind,  # type: ignore[arg-type]
        name=name,
        version=version,
        interface=interface,
        inputs=frozenset(inputs),
        provides=frozenset(provides),
        preconditions=tuple(preconditions),
        cost=CostEstimate(
            tokens=tokens, latency_ms=latency_ms, external_calls=external_calls
        ),
        scenarios=tuple(scenarios),
        metrics=dict(metrics or {"quality": "任务成功率"}),
        available=available,
        unavailable_reason=unavailable_reason,
    )


#: 组件接口名。**它们进声明、进配置、进评测记录**（`NFR-ST-09`）。
IFACE = {
    "context": "ContextComponent",
    "memory": "MemoryComponent",
    "retrieval": "RetrievalComponent",
    "tool_selection": "ToolSelectionComponent",
    "permission": "PermissionComponent",
    "validation": "ValidationComponent",
    "execution": "ExecutionComponent",
}


def builtin_specs() -> tuple[StrategySpec, ...]:
    """一期登记的全部声明。

    ``provides`` / ``inputs`` 不是装饰：**契约规则正是读它们做判断的** ——
    「只读权限 × 需要写权限的工具」那条，靠的就是 ``tool_selection`` 声明
    ``inputs={"write_access"}``、而 ``permission`` 只在非只读档提供它。
    新增一条权限档位或工具选择策略时，那条规则**不需要改**（架构文档 §3.2）。

    ⚠ **词汇表只表达"策略 ← 策略"的依赖**，不表达基础设施依赖。
    例如"向量检索需要 embeddings"就不该写成 ``inputs`` —— embeddings 来自
    gateway 与向量库，不是另一个**策略**提供的；写进来会让它永远无人满足，
    而五条规则里没有一条会拦这种"孤立的输入"（那会让 vector/hybrid 检索永远选不中，
    且不报错）。目前词汇表里的四个输入 —— ``write_access`` / ``task_state`` /
    ``memory_content`` / ``multiple_candidates`` —— 每一个都有对应的规则管着。
    """
    return (
        # ---------------- 上下文管理（`src/context` 真有的两条）--------------
        _spec(
            "context", "linear", interface=IFACE["context"],
            provides=("context_window",),
            tokens=3_000, latency_ms=50,
            scenarios=("long_task", "长任务", "单任务", "代码", "case_generation"),
            metrics={"quality": "任务成功率", "cost": "token 用量"},
        ),
        _spec(
            "context", "tiered", interface=IFACE["context"],
            # tiered 有会话摘要链，能跨轮保住任务状态；linear 只压中段，不保证
            provides=("context_window", "task_state"),
            tokens=4_500, latency_ms=80,
            scenarios=("multi_turn", "多轮", "跨会话", "长期", "kb_qa"),
            metrics={"quality": "任务成功率", "cost": "token 用量"},
        ),
        _spec(
            "context", "full", interface=IFACE["context"],
            provides=("context_window", "task_state"),
            tokens=12_000, latency_ms=20,
            scenarios=("short", "短对话", "一次性"),
            metrics={"quality": "信息完整率"},
        ),
        # ---------------- 记忆机制（`src/memory`）---------------------------
        _spec(
            "memory", "stateless", interface=IFACE["memory"],
            tokens=0, latency_ms=0,
            scenarios=("one_shot", "单次问答", "无状态"),
            metrics={"quality": "任务成功率"},
        ),
        _spec(
            "memory", "session", interface=IFACE["memory"],
            provides=("memory_content",),
            tokens=600, latency_ms=120,
            scenarios=("continuous", "连续调试", "会话内"),
            metrics={"quality": "经验命中率"},
        ),
        _spec(
            "memory", "structured", interface=IFACE["memory"],
            # **它需要上下文保住 task_state** —— 正是 `state_compression_loses_memory_fields`
            # 那条规则要挡的地方。二期把「多级摘要」之类真会丢字段的策略登记上来时，
            # 这条依赖就会立刻生效。
            provides=("memory_content", "structured_memory"),
            inputs=("task_state",),
            tokens=900, latency_ms=150,
            scenarios=("fault_reuse", "故障复用", "经验"),
            metrics={"quality": "经验命中率", "safety": "闸门拦截率"},
        ),
        # ---------------- 检索策略 ------------------------------------------
        _spec(
            "retrieval", "vector", interface=IFACE["retrieval"],
            provides=("evidence",),
            tokens=300, latency_ms=90, external_calls=1,
            scenarios=("tech_qa", "技术问答", "语义"),
            metrics={"quality": "引用命中率"},
        ),
        _spec(
            "retrieval", "hybrid", interface=IFACE["retrieval"],
            provides=("evidence",),
            tokens=400, latency_ms=120, external_calls=1,
            scenarios=("kb_qa", "知识问答", "混合"),
            metrics={"quality": "引用命中率"},
        ),
        _spec(
            "retrieval", "keyword", interface=IFACE["retrieval"],
            provides=("evidence",),
            tokens=100, latency_ms=20,
            scenarios=("exact", "精确参数查询", "标识符"),
            metrics={"quality": "精确命中率"},
        ),
        _spec(
            "retrieval", "case_kb", interface=IFACE["retrieval"],
            # **它要吃记忆内容** —— `stateless_conflicts_with_structured_memory`
            # 那条规则抓的就是"记忆选成 stateless、而检索还要记忆内容"。
            provides=("evidence",), inputs=("memory_content",),
            tokens=500, latency_ms=140, external_calls=1,
            scenarios=("fault_diagnosis", "故障分析", "历史案例"),
            metrics={"quality": "案例命中率"},
        ),
        # ---------------- 工具选择 ------------------------------------------
        _spec(
            "tool_selection", "fixed", interface=IFACE["tool_selection"],
            tokens=0, latency_ms=0,
            scenarios=("single_op", "单一操作", "窄任务"),
            metrics={"quality": "选错率"},
        ),
        _spec(
            "tool_selection", "category", interface=IFACE["tool_selection"],
            # 按类别筛会带出写工具 → **需要写权限**
            inputs=("write_access",),
            tokens=150, latency_ms=15,
            scenarios=("complex", "复杂任务", "多工具"),
            metrics={"quality": "选错率"},
        ),
        _spec(
            "tool_selection", "dependency_based", interface=IFACE["tool_selection"],
            inputs=("write_access",),
            tokens=250, latency_ms=30,
            scenarios=("environment", "环境操作", "依赖"),
            metrics={"quality": "选错率"},
        ),
        # ---------------- 权限管理 ------------------------------------------
        _spec(
            "permission", "readonly", interface=IFACE["permission"],
            tokens=0, latency_ms=0,
            scenarios=("knowledge", "知识查询", "只读"),
            metrics={"safety": "越权次数"},
        ),
        _spec(
            "permission", "restricted", interface=IFACE["permission"],
            # **只有非只读档才提供写权限** —— 那条约束就是这么判的
            provides=("write_access",),
            tokens=0, latency_ms=0,
            scenarios=("environment", "环境操作", "受限写入"),
            metrics={"safety": "越权次数"},
        ),
        _spec(
            "permission", "sandbox", interface=IFACE["permission"],
            provides=("write_access",),
            tokens=0, latency_ms=0,
            scenarios=("automation", "自动化", "沙箱"),
            metrics={"safety": "逃逸次数"},
        ),
        # ---------------- 验证机制 ------------------------------------------
        _spec(
            "validation", "format", interface=IFACE["validation"],
            provides=("checked",),
            tokens=0, latency_ms=5,
            scenarios=("case_generation", "用例生成", "格式"),
            metrics={"quality": "Schema 合法率"},
        ),
        _spec(
            "validation", "rules", interface=IFACE["validation"],
            provides=("checked",),
            tokens=50, latency_ms=20,
            scenarios=("execution", "测试执行", "规则"),
            metrics={"quality": "规则通过率"},
        ),
        # ---------------- 执行策略 ------------------------------------------
        _spec(
            "execution", "single", interface=IFACE["execution"],
            tokens=0, latency_ms=0,
            scenarios=("simple", "简单任务", "单次"),
            metrics={"quality": "任务成功率"},
        ),
        _spec(
            "execution", "retry", interface=IFACE["execution"],
            provides=("retried",),
            tokens=200, latency_ms=300,
            scenarios=("flaky", "易失败任务", "重试"),
            metrics={"quality": "恢复率"},
        ),
        _spec(
            "execution", "compare", interface=IFACE["execution"],
            # **要多个候选，而当前没有任何组件产出它们** ——
            # 于是这套组合永远过不了契约（`CST-8` 要的正是这个行为）。
            inputs=("multiple_candidates",),
            tokens=1_500, latency_ms=1_200,
            scenarios=("diagnosis", "复杂诊断", "候选比较"),
            metrics={"quality": "最优候选命中率"},
        ),
    )


#: 一期**点名但不假装有**的策略：登记为 unavailable + 原因（`FR-ST-01` 的验收点）。
#: 它们之所以要登记，是因为"为什么不能用它"是一个会被问的问题。
_PLANNED: tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("validation", "citation_check", "引用验证（知识库问答）", ("kb_qa", "知识问答"),
     ("需要引用与来源的比对实现（尚未实现）",)),
    ("validation", "constraint_check", "约束校验（用例生成）", ("case_generation", "用例生成"),
     ("需要需求点 ↔ 用例的覆盖矩阵（尚未实现）",)),
    ("validation", "env_state_check", "环境状态检查", ("environment", "环境操作"),
     ("需要环境探针（尚未实现）",)),
    ("validation", "log_evidence", "日志证据验证", ("fault_diagnosis", "故障分析"),
     ("需要日志规则引擎（尚未实现）",)),
    ("validation", "cross_validation", "多 Agent 交叉验证", ("cross_validation", "交叉验证"),
     ("multiagent 未启用 —— 它会安静地退化成「只有一个 agent 的结论」",)),
    ("execution", "backtrack", "回溯（假设—验证—回溯）", ("fault_diagnosis", "故障分析"),
     ("需要候选假设与回溯执行（尚未实现）",)),
    ("retrieval", "query_decomposition", "查询分解", ("fault_diagnosis", "复杂故障分析"),
     ("需要查询分解与子查询合并（尚未实现）",)),
    ("retrieval", "sql", "SQL 检索", ("exact", "参数查询"),
     ("需要结构化检索后端（尚未实现）",)),
    ("retrieval", "bm25", "BM25 检索", ("exact", "关键词"),
     ("当前只有向量 + 关键词的混合检索，没有独立的 BM25 索引（未实现）",)),
    ("permission", "approval", "审批后执行", ("environment", "高危操作"),
     ("需要审批通道（尚未实现）",)),
)


def _planned_specs() -> tuple[StrategySpec, ...]:
    return tuple(
        # 标成不可用 —— **保留在册**并带上原因（`FR-ST-05`）。
        # 不登记的话，"为什么不能用它"就变成一个查不到的空缺。
        _spec(
            kind, name, interface=IFACE[kind], scenarios=scenarios,
            preconditions=reasons, metrics={"quality": "待实现"},
            available=False, unavailable_reason=f"{label}：{reasons[0]}",
        )
        for kind, name, label, scenarios, reasons in _PLANNED
    )


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def build_strategy(
    *,
    strategy_cfg: Mapping[str, Any] | None,
    events: Any | None = None,
    clock: Any | None = None,
    evaluated: EvaluatedSource | None = None,
) -> StrategyBundle | None:
    """按 ``strategy`` 段装配。``strategy.enabled=false`` 时返回 ``None``。"""
    cfg = dict(strategy_cfg or {})
    if not bool(cfg.get("enabled", True)):
        _log.info("组件策略层未启用（strategy.enabled=false）")
        return None

    registry = StrategyRegistry()
    for spec in (*builtin_specs(), *_planned_specs()):
        registry.register(spec)

    contracts_cfg = cfg.get("contracts") if isinstance(cfg.get("contracts"), Mapping) else {}
    if not bool((contracts_cfg or {}).get("enabled", True)):
        # `C-ST-1`：它关掉的是本模块存在的理由 —— 必须**告警**，不许静默。
        _log.warning(
            "strategy.contracts.enabled=false —— **契约校验已关闭**。\n"
            "它是这一层存在的理由：关掉之后，「只读权限 + 需要写入的工具」"
            "「任务状态压缩 + 结构化经验记忆」这类**沉默的冲突**不会再被挡住，"
            "而它们的表现是「这个任务怎么老失败」，离原因非常远。"
        )

    policy = SelectionPolicy.from_config(cfg)
    guard = cfg.get("guard") if isinstance(cfg.get("guard"), Mapping) else {}
    if not policy.high_risk_requires_validated:
        # `C-ST-3`：那意味着高风险任务可以用没验证过的组合 —— 必须告警。
        _log.warning(
            "strategy.guard.high_risk_requires_validated=false —— "
            "**高风险任务可以使用未经验证的组合**。"
            "安全约束本应是硬约束（不能被更低的成本抵掉），请确认这是有意为之。"
        )
    if not policy.enabled:
        _log.warning("strategy.enabled=false —— 选择器不会工作，调用方应退回默认配置")

    templates, issues = build_templates(cfg.get("templates"), registry=registry)
    if not templates:
        _log.warning(
            "没有配置任何场景模板（strategy.templates 为空）—— "
            "选择器将没有降级目标，也没有默认路径。"
        )

    contracts = default_contracts(registry)
    selector = StrategySelector(
        registry=registry,
        contracts=contracts,
        templates=templates,
        policy=policy,
        evaluated=evaluated or _NoEvaluatedData(),
        events=events,
        clock=clock,
    )

    available = sum(1 for spec in registry.all() if spec.available)
    _log.info(
        "组件策略层装配完成：%d 条策略（可用 %d / 不可用 %d），%d 条契约规则，"
        "%d 个场景模板，默认配置=%s",
        len(registry), available, len(registry) - available,
        len(contracts), len(templates), policy.default_profile,
    )
    _report_combo_metric(selector)
    return StrategyBundle(selector=selector, registry=registry, policy=policy, issues=issues)


def _report_combo_metric(selector: StrategySelector) -> None:
    """把「合法组合有多少个」在**启动时**报出来（`MST-12` / `NFR-ST-08`）。

    **为什么要专门为它写一段**：约束的松紧**没有别的观测手段** ——
    组合配错了只会在运行时以「这个任务怎么老失败」的形式出现，
    而那离"契约没写紧"这个原因非常远。

    所以这条指标必须**每次启动都被看见**，而不是只活在某个测试的断言里：

    - 两位数到三位数 → 约束写得够紧；
    - **四位数** → 说明某条契约形同虚设，该补规则了；
    - 枚举不出来 → 某条契约压根没生效。
    """
    try:
        valid = selector.valid_profiles()
    except Exception as exc:  # noqa: BLE001 — 指标算不出来不该阻断启动
        _log.warning("合法组合枚举失败（约束指标不可用）：%s", exc)
        return
    count = len(valid)
    if count == 0:
        _log.warning(
            "**合法组合数为 0** —— 没有任何一组配置能通过契约校验。"
            "这意味着所有任务都会走降级（或高风险任务直接被拒）。"
            "请检查契约规则是否过紧，或注册表里是否有可用的策略。"
        )
    elif count >= 1000:
        _log.warning(
            "合法组合数 %d —— **落在四位数**。按本层的判据，这说明**约束没写紧**"
            "（目标量级是两位数到三位数）：四位数意味着契约几乎没有排除掉什么，"
            "而「组合的错是沉默的错」正是这一层要挡的东西。\n"
            "该补契约规则了（新增一条 = 加一个函数 + 一条登记，不改校验器）。",
            count,
        )
    else:
        _log.info("合法组合数 %d —— 在两位数到三位数的目标区间内。", count)


def describe(selector: StrategySelector) -> Mapping[str, Any]:
    """给 ``doctor`` 用：七个组件、每个下面的可用与不可用（`FR-ST-01` 的验收点）。"""
    return {
        "components": selector.registry.to_dict(),
        "templates": [t.name for t in selector.templates],
        "default_profile": selector.policy.default_profile,
    }


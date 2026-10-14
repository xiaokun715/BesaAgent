"""场景模板与裁剪（`FR-ST-13` / `FR-ST-14`）—— 组合爆炸的**第二种控制手段**。

模板是**数据**（进配置，新增模板不改代码），它表达的是：

    这套场景**固定**用哪几条策略；其余组件沿用默认配置（baseline）。

**为什么模板是默认路径**：它是唯一**零成本、可解释、且经过人工设计**的一层。
选择器三层（模板 → 规则 → 评测）里它排第一，理由与"上下文的 L1 排在 L2 前"、
"路由的规则排在 LLM 前"完全一样：**能零成本解决的，就不要花钱**。

## 裁剪的语义（这里要说清楚，否则会各自理解）

需求原文的裁剪例子是「已有测试用例 → 需求分析与用例生成不必启用」——
但**需求分析与用例生成是 agent 的阶段，不是本层的组件**。
在本层的七个组件上，"去掉一个环节"的正确表达是：

> **把该组件回退到 baseline 的那一档** —— 即"这次不需要为它做特殊配置"。

于是裁剪 = **选择性放弃模板的专门配置**，而不是"删掉一个绑定"
（绑定删了会让 profile 不完整，而"不完整"在契约校验里没有含义）。
裁剪依据（哪些资产导致了哪几项回退）**必须进选择记录**（`FR-ST-14`）。

## 风险不走裁剪

高风险任务的收紧**不靠模板调整** —— 那会让渲染出来的 profile 落在
"已验证集合"之外，从而触发 `FR-ST-08` 的拒绝。风险走的是**候选集切分**（见 `selector`），
不是"把某个组件换个值"。这两件事分开，是刻意的。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from strategy.registry import StrategyRegistry
from strategy.types import (
    COMPONENT_KINDS,
    ComponentKind,
    RiskLevel,
    TaskFeatures,
)

__all__ = ["Template", "build_templates", "match_template", "render_template"]

_log = logging.getLogger(__name__)

#: 保留名：默认配置。**它不是"一个场景"**，而是降级目标与兜底（`FR-ST-16`）。
BASELINE_NAME = "baseline"


@dataclass(frozen=True)
class Template:
    """一个场景模板。``bindings`` 只写**这个场景要固定**的那几个组件。"""

    name: str
    risk: RiskLevel = "low"
    bindings: Mapping[ComponentKind, str] = field(default_factory=dict)
    #: 输入资产 → 该资产出现时**回退到 baseline** 的组件（`FR-ST-14`）
    trim: Mapping[str, tuple[ComponentKind, ...]] = field(default_factory=dict)
    description: str = ""

    @property
    def is_baseline(self) -> bool:
        return self.name == BASELINE_NAME


def build_templates(
    cfg: Mapping[str, Any] | None,
    *,
    registry: StrategyRegistry | None = None,
) -> tuple[tuple[Template, ...], tuple[str, ...]]:
    """从配置构造模板。

    Returns:
        ``(模板列表, 问题列表)``。问题**不阻断启动**（与"工具体检失败不阻断启动"
        同一条纪律），但调用方**必须把它们告警出来** —— `C-ST-6` 要的是
        "模板引用了不存在的策略时启动期告警"，而不是静默。

    配置形状（`configs/base.yaml` 的 ``strategy.templates``）::

        kb_qa:
          risk: low
          description: 知识库问答
          context: multi_level_summary
          retrieval: hybrid
          trim: {has_test_cases: [retrieval]}
    """
    data = dict(cfg or {})
    templates: list[Template] = []
    issues: list[str] = []

    for name, raw in data.items():
        if not isinstance(raw, Mapping):
            issues.append(f"模板 {name!r} 的配置不是一个映射，已忽略")
            continue
        bindings: dict[ComponentKind, str] = {}
        for key, value in raw.items():
            if key in ("risk", "description", "trim", "trimmable"):
                continue
            if key not in COMPONENT_KINDS:
                # 组件类别名是契约（`NFR-ST-09`）—— 拼错要立刻说，
                # 否则它会被当成"这个模板没配这一项"，而后果是那条配置**完全没生效**。
                issues.append(
                    f"模板 {name!r} 里的 {key!r} 不是组件类别"
                    f"（已知：{', '.join(COMPONENT_KINDS)}），该行已忽略"
                )
                continue
            strategy = str(value or "").strip()
            if not strategy:
                continue
            if registry is not None and not registry.has(key, strategy):  # type: ignore[arg-type]
                # `C-ST-6`：引用不存在的策略 → **启动期告警**。
                # 不在这里硬失败：一个模板写错不该让整个进程起不来，
                # 但它必须在启动时被看见 —— 渲染时还会再记一笔（见 render_template）。
                issues.append(
                    f"模板 {name!r} 引用了未注册的策略 {key}.{strategy}"
                    f"（该类别已登记：{', '.join(s.name for s in registry.by_kind(key)) or '无'}）"  # type: ignore[arg-type]
                )
            bindings[key] = strategy  # type: ignore[index]

        trim: dict[str, tuple[ComponentKind, ...]] = {}
        raw_trim = raw.get("trim")
        if isinstance(raw_trim, Mapping):
            for asset, kinds in raw_trim.items():
                if isinstance(kinds, str):
                    kinds = (kinds,)
                trim[str(asset)] = tuple(
                    kind for kind in kinds if kind in COMPONENT_KINDS  # type: ignore[misc]
                )
                unknown = [k for k in kinds if k not in COMPONENT_KINDS]  # type: ignore[union-attr]
                if unknown:
                    issues.append(
                        f"模板 {name!r} 的 trim[{asset!r}] 里有未知组件：{', '.join(map(str, unknown))}"
                    )

        risk = str(raw.get("risk") or "low")
        if risk not in ("low", "high"):
            issues.append(f"模板 {name!r} 的 risk={risk!r} 非法（只能是 low / high），按 low 处理")
            risk = "low"

        templates.append(
            Template(
                name=name,
                risk=risk,  # type: ignore[arg-type]
                bindings=bindings,
                trim=trim,
                description=str(raw.get("description") or ""),
            )
        )

    for issue in issues:
        _log.warning("场景模板配置：%s", issue)
    return tuple(templates), tuple(issues)


def match_template(
    templates: Sequence[Template], features: TaskFeatures
) -> Template | None:
    """按 ``features.template_key`` 找模板。**找不到返回 ``None``**（交给下一层）。

    ``baseline`` **不参与按任务类型的匹配** —— 它是降级目标，
    不是一个"任务类型"；让它参与匹配会让"没命中任何场景"被伪装成"命中了 baseline"。
    """
    key = features.template_key.strip()
    if not key:
        return None
    for template in templates:
        if template.name == key and not template.is_baseline:
            return template
    return None


def render_template(
    template: Template,
    *,
    baseline: Mapping[ComponentKind, str],
    features: TaskFeatures,
    registry: StrategyRegistry | None = None,
) -> tuple[dict[ComponentKind, str], tuple[str, ...]]:
    """把模板渲染成一份完整绑定表：**baseline 打底 + 模板覆盖 + 按资产裁剪**。

    Returns:
        ``(绑定表, 说明)``。说明里逐条记下"裁剪掉了什么、因为哪个资产"——
        `FR-ST-14` 要求裁剪结果可追溯，而这几行就是选择记录里的依据。
    """
    bindings: dict[ComponentKind, str] = dict(baseline)
    for kind, name in template.bindings.items():
        if registry is not None and not registry.has(kind, name):
            # 启动期已经告警过一次；这里再记一笔，让**这一次选择**的理由里也有它 ——
            # 否则排障时会看到"模板明明配了 X，怎么没用上"。
            continue
        bindings[kind] = name

    notes: list[str] = []
    for asset in sorted(features.input_assets):
        for kind in template.trim.get(asset, ()):
            before = bindings.get(kind, "")
            back = baseline.get(kind, "")
            if before == back:
                continue
            bindings[kind] = back
            notes.append(
                f"因为输入资产 {asset}，{kind} 从「{before}」回退到默认的「{back}」"
            )
    return bindings, tuple(notes)

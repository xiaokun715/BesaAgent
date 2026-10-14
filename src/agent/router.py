"""阶段路由：任务 → 七个阶段之一（`FR-A-02`）。

**顺序即优先级，且这个顺序是有理由的**（`MA-10`）：

```
显式指定  →  规则打分  →  LLM 判断  →  on_unknown 策略
  ↑ 永远优先    ↑ 零成本、可解释      ↑ 花钱、会漂      ↑ 可配置，无默认值
```

- **显式永远优先**：调用方说跑哪个阶段就跑哪个，哪怕任务描述看起来像别的阶段。
  替调用方"纠正"它，是这类系统里最容易让人失去信任的一种聪明；
- **规则排在 LLM 前面**：与压缩的 L1 排在 L2 前同一个思路 ——
  能零成本且可解释地解决的，就不要花钱；
- **LLM 判断永远不是必需品**：它是可选的，失败或低置信度都退到 `on_unknown`，
  而不是抛错。一个可选的增强不该成为必需路径。

**推断不出来时不许瞎猜**（`MA-11`）：按 `on_unknown` 走 ——
交互式可以问（`ask`）、批处理必须拒绝（`reject`）、也可以指定兜底（`default`，
但**要告警**）。三种在不同场景下都是对的，所以不替配置者决定。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.stages import StageRegistry

__all__ = ["RoutingDecision", "StageRouter", "UnknownPolicy"]

#: 推断不出阶段时的行为（`C-A-1`：**没有默认值**）。
UnknownPolicy = Literal["ask", "default", "reject"]

_log = logging.getLogger(__name__)

#: 规则层的及格线：最高分要 ≥ 它才算"判断出来了"。**它可配**（`agent.routing.rule_threshold`）。
_DEFAULT_THRESHOLD = 0.5

#: 命中多少个关键词算"满分"。
#:
#: ⚠ **分母用这个固定数，而不是该阶段的关键词总数**。用总数会得到一个荒谬的激励：
#: 关键词列得越细的阶段越难被选中 —— 实测里 `test_case` 命中了「用例」「测试用例」两个词，
#: 却因为有 6 个关键词只拿到 0.33 分，**过不了线**。
#: 于是"把关键词写全一点"这件本该是好事的事，反而让它选不中。
#:
#: 取 2 的意思是：命中一个词 = 0.5（刚好过线），命中两个 = 1.0（确信）。
_DEFAULT_SATURATION = 2


@dataclass(frozen=True)
class RoutingDecision:
    """一次路由的结论。**理由与候选都要带** —— 没有理由的路由在排障时等于没有信息。"""

    stage: str = ""
    reason: str = ""
    confidence: float = 0.0
    #: 没选中的那些（按分数降序），供排障看"差多少"
    candidates: tuple[str, ...] = ()
    #: ``on_unknown="ask"`` 时为真：**需要调用方指定阶段**。
    needs_input: bool = False
    #: 各阶段的规则得分（仅规则层有值时给出）
    scores: Mapping[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.stage) and not self.needs_input


class StageRouter:
    """阶段路由器。**每个依赖都可缺省** —— 缺了就退到下一层。"""

    __slots__ = (
        "_registry", "_on_unknown", "_default_stage", "_use_llm", "_chat",
        "_threshold", "_saturation",
    )

    def __init__(
        self,
        registry: StageRegistry,
        *,
        on_unknown: UnknownPolicy,
        default_stage: str = "",
        use_llm: bool = False,
        chat: Any | None = None,
        rule_threshold: float = _DEFAULT_THRESHOLD,
        saturation_hits: int = _DEFAULT_SATURATION,
    ) -> None:
        if on_unknown not in ("ask", "default", "reject"):
            raise ValueError(
                f"agent.routing.on_unknown 只能是 ask | default | reject，得到 {on_unknown!r}"
            )
        if on_unknown == "default" and not default_stage:
            raise ValueError("agent.routing.on_unknown=default 时必须给 default_stage")
        if on_unknown == "default" and not registry.has(default_stage):
            raise ValueError(
                f"agent.routing.default_stage={default_stage!r} 不在已注册的阶段里："
                f"{', '.join(registry.names()) or '（无）'}"
            )
        self._registry = registry
        self._on_unknown = on_unknown
        self._default_stage = default_stage
        self._use_llm = use_llm
        self._chat = chat
        self._threshold = rule_threshold
        self._saturation = max(1, saturation_hits)

    # ---------------------------------------------------------------- 入口
    async def route(
        self, task: str, *, stage: str | None = None, hint: str = ""
    ) -> RoutingDecision:
        """把任务路由到一个阶段。"""
        # ① 显式指定 —— **永远优先**（哪怕它看起来像别的阶段）
        if stage:
            if not self._registry.has(stage):
                raise ValueError(
                    f"显式指定的阶段 {stage!r} 不在已注册的阶段里："
                    f"{', '.join(self._registry.names()) or '（无）'}"
                )
            return RoutingDecision(
                stage=stage, reason="调用方显式指定", confidence=1.0
            )

        # ② 规则打分（零成本、可解释）
        decision = self._by_rules(task, hint)
        if decision is not None:
            return decision

        # ③ 可选的 LLM 判断
        if self._use_llm and self._chat is not None:
            llm_decision = await self._by_llm(task)
            if llm_decision is not None:
                return llm_decision

        # ④ 推断不出来 —— 按策略
        return self._unknown(task)

    # ---------------------------------------------------------------- 规则
    def _by_rules(self, task: str, hint: str) -> RoutingDecision | None:
        text = f"{task}\n{hint}".casefold()
        if not text.strip():
            return None

        scores: dict[str, float] = {}
        for agent in self._registry.all():
            keywords = getattr(agent, "keywords", ()) or ()
            if not keywords:
                continue
            hits = sum(1 for word in keywords if word.casefold() in text)
            # 分母是**固定的饱和数**，不是关键词总数 —— 见 ``_DEFAULT_SATURATION``
            scores[agent.stage] = min(1.0, hits / self._saturation)

        if not scores:
            return None

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        best_stage, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        # 并列或都不过线 → **不猜**，交给下一层
        if best_score < self._threshold or best_score <= runner_up:
            return None

        return RoutingDecision(
            stage=best_stage,
            reason=f"规则命中「{best_stage}」的关键词，得分 {best_score:.2f}（次高 {runner_up:.2f}）",
            confidence=best_score,
            candidates=tuple(name for name, _ in ranked[1:]),
            scores=scores,
        )

    # ---------------------------------------------------------------- LLM
    async def _by_llm(self, task: str) -> RoutingDecision | None:
        """一次补全判断阶段。**失败或低置信度都返回 ``None``**（退到下一层）。"""
        stages = self._registry.names()
        system = (
            "判断下面这个任务属于软件测试的哪个阶段。只回答 JSON：\n"
            f'{{"stage": "<{ " | ".join(stages) }>", "confidence": <0..1>, "reason": "<一句话>"}}\n'
            "拿不准时把 confidence 给低 —— **宁可说拿不准，不要硬猜**。"
        )
        schema = {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "enum": list(stages)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reason": {"type": "string"},
            },
            "required": ["stage"],
        }
        try:
            raw = await self._chat.complete(
                system=system, prompt=task, json_schema=schema, max_tokens=128, temperature=0.0
            )
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001 — 可选增强失败要降级
            _log.warning("阶段路由的 LLM 判断不可用，退到 on_unknown 策略：%s", exc)
            return None

        if not isinstance(data, Mapping):
            return None
        chosen = str(data.get("stage") or "")
        if not self._registry.has(chosen):
            return None
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < self._threshold:
            # 低置信度**不采用** —— 让它去走 on_unknown，而不是拿一个"可能对"的结果当结论
            return None
        return RoutingDecision(
            stage=chosen,
            reason=f"LLM 判断：{data.get('reason') or '（无理由）'}",
            confidence=confidence,
            candidates=tuple(name for name in stages if name != chosen),
        )

    # ---------------------------------------------------------------- 兜底
    def _unknown(self, task: str) -> RoutingDecision:
        known = self._registry.names()
        listed = ", ".join(known) or "（无）"

        if self._on_unknown == "default":
            # **告警**：用一个兜底阶段跑，和"判断出就是这个阶段"是两回事。
            _log.warning(
                "任务无法路由到确定的阶段，按配置使用兜底阶段 %r。"
                "如果这不是你要的，请显式指定 stage 或调整 agent.routing.on_unknown。",
                self._default_stage,
            )
            return RoutingDecision(
                stage=self._default_stage,
                reason=f"**推断不出阶段**（任务与任何阶段的关键词都不匹配），按配置使用兜底 {self._default_stage}",
                confidence=0.0,
                candidates=known,
            )

        if self._on_unknown == "reject":
            raise ValueError(
                f"任务无法路由到任何已注册的阶段（{listed}）。"
                "请显式指定 stage，或把 agent.routing.on_unknown 改为 ask / default。"
            )

        # ask：**"需要指定"是一种正常结果，不是错误**（所以不抛异常）
        return RoutingDecision(
            reason=f"任务无法路由到确定的阶段（可用：{listed}）—— 请指定 stage",
            confidence=0.0,
            candidates=known,
            needs_input=True,
        )

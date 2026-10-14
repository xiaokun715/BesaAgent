"""策略选择：**模板 → 规则 → 评测 → 降级**（`FR-ST-10`），外加一道安全闸门。

四层顺序即优先级，理由与别处一致（`MST-6`）：

| 层 | 成本 | 可解释 | 数据依赖 |
|---|---|---|---|
| **模板** | 零 | 是（人工设计过） | 无 |
| **规则** | 零 | 是（命中哪些 scenario） | 无 |
| **评测** | 需历史数据 | 是（分数可读） | **有** |
| **降级** | 零 | 是（说清试过哪些层） | 无 —— 它是**失败时的落点**，不是一层选择 |

**能零成本且可解释地解决的，就不要花钱** —— 与上下文的 L1 排在 L2 前、
agent 路由的规则排在 LLM 前是同一条纪律。

## 两条打不住的线

1. **高风险任务只在「合法 ∩ 已验证」里选**（`FR-ST-08`）。这是**集合运算**，
   不是目标函数里的一项 —— 加权意味着安全**可以被更低的成本抵掉**，
   而"这次便宜 20%，但用没验证过的配置去动生产环境"不是一笔可以做的交易。
   没有已验证组合时**拒绝执行**，不是降级到未验证的组合。
2. **评测结论必须版本对得上**（`FR-ST-11`）。拿旧版的结论套新实现，是
   "**看起来有依据**"的决策 —— 它比没有依据更危险，因为它不会被怀疑。

本层**只发事件、不落库**：选择留痕归 ``src/repo``（经事件），评测数据由组合根注入。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from foundation.clock import Clock, SystemClock

from strategy.assembler import build_profile
from strategy.contracts import Contract, enumerate_valid, validate
from strategy.registry import StrategyRegistry
from strategy.templates import BASELINE_NAME, Template, match_template, render_template
from strategy.types import (
    COMPONENT_KINDS,
    AgentProfile,
    Candidate,
    ComponentKind,
    EvalRecord,
    Layer,
    SelectionDecision,
    TaskFeatures,
    parse_profile_key,
)

__all__ = [
    "DEFAULT_EVENTS",
    "EvaluatedSource",
    "EventSink",
    "NullEventSink",
    "SelectionPolicy",
    "StrategySelector",
]

_log = logging.getLogger(__name__)


class EventSink(Protocol):
    """事件出口。与 gateway / tool / memory / context / agent 同形状。"""

    def emit(self, name: str, payload: Mapping[str, Any]) -> None: ...


class NullEventSink:
    """默认出口。**不是静默，而是让"没接事件"是一个明示的选择。**"""

    __slots__ = ()

    def emit(self, name: str, payload: Mapping[str, Any]) -> None:  # noqa: D102
        return None


class EvaluatedSource(Protocol):
    """评测数据的来源。**由组合根注入**（本层不落库，`NFR-ST-02`）。

    只定义这一个方法，是为了让单测能塞一个确定性的假实现 ——
    选择的正确性不该依赖数据库里恰好有什么。
    """

    def records(self) -> Sequence[EvalRecord]: ...


#: 本层的事件（架构文档 §8）。``<模块>.<对象>.<动作>``。
#: 哪些契约规则属于"不可用导致的拒绝" —— 除了那个专门管交叉验证的规则，
#: 还有兜底那条 `unavailable_strategy`（任何不可用策略被选进组合都走它）。
_UNAVAILABLE_RULES = frozenset({"cross_validation_needs_multiagent", "unavailable_strategy"})

DEFAULT_EVENTS = {
    "selected": "strategy.selected",
    "combo_rejected": "strategy.combo_rejected",
    "degraded": "strategy.degraded",
    "unavailable": "strategy.unavailable",
    "template_matched": "strategy.template_matched",
    "evaluated": "strategy.evaluated",
    "high_risk_refused": "strategy.high_risk_refused",
}


@dataclass(frozen=True)
class SelectionPolicy:
    """``strategy`` 段的参数。"""

    enabled: bool = True
    rules: bool = True
    evaluated: bool = True
    #: λc / λt（`C-ST-4`）：**只作用于非高风险任务**
    weight_cost: float = 0.3
    weight_latency: float = 0.2
    #: 样本少于它就当"没有数据"
    min_samples: int = 20
    #: 高风险只在已验证组合里选（`FR-ST-08`）。关掉要告警（`C-ST-3`）
    high_risk_requires_validated: bool = True
    #: 降级目标（`FR-ST-16`）
    default_profile: str = BASELINE_NAME
    #: 一次枚举/评测的组合数上界（`C-ST-5`）
    max_combos_per_run: int = 20
    #: 规则层的及格线（与 agent 路由同一个思路：分母是饱和数，不是关键词总数）
    rule_saturation: int = 2

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "SelectionPolicy":
        data = dict(cfg or {})
        selection = data.get("selection") if isinstance(data.get("selection"), Mapping) else {}
        guard = data.get("guard") if isinstance(data.get("guard"), Mapping) else {}
        evaluation = data.get("evaluation") if isinstance(data.get("evaluation"), Mapping) else {}
        weights = selection.get("weights") if isinstance(selection.get("weights"), Mapping) else {}
        policy = cls(
            enabled=bool(data.get("enabled", True)),
            rules=bool(selection.get("rules", True)),
            evaluated=bool(selection.get("evaluated", True)),
            weight_cost=float(weights.get("cost", 0.3)),
            weight_latency=float(weights.get("latency", 0.2)),
            min_samples=int(selection.get("min_samples", 20)),
            high_risk_requires_validated=bool(guard.get("high_risk_requires_validated", True)),
            default_profile=str(data.get("default_profile") or BASELINE_NAME),
            max_combos_per_run=int(evaluation.get("max_combos_per_run", 20)),
            rule_saturation=int(selection.get("rule_saturation", 2)),
        )
        policy._validate()
        return policy

    def _validate(self) -> None:
        if self.weight_cost < 0 or self.weight_latency < 0:
            raise ValueError("strategy.selection.weights 不能为负")
        if self.min_samples < 0:
            raise ValueError("strategy.selection.min_samples 不能为负")
        if self.max_combos_per_run <= 0:
            # `C-ST-5`：没有它，一次评测能跑到天亮。
            raise ValueError("strategy.evaluation.max_combos_per_run 必须为正 —— 没有「不限」这个选项")
        if self.rule_saturation < 1:
            raise ValueError("strategy.selection.rule_saturation 必须 ≥ 1")


class StrategySelector:
    """三层 + 一个降级终点。**纯逻辑，无 IO**（评测数据是注入进来的）。"""

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        contracts: Sequence[Contract],
        templates: Sequence[Template] = (),
        policy: SelectionPolicy | None = None,
        evaluated: EvaluatedSource | None = None,
        events: EventSink | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._contracts = tuple(contracts)
        self._templates = tuple(templates)
        self._policy = policy or SelectionPolicy()
        self._evaluated = evaluated
        self._events = events or NullEventSink()
        self._clock = clock or SystemClock()
        self._valid_cache: tuple[AgentProfile, ...] | None = None

    # ---------------------------------------------------------------- 只读
    @property
    def registry(self) -> StrategyRegistry:
        return self._registry

    @property
    def policy(self) -> SelectionPolicy:
        return self._policy

    @property
    def templates(self) -> tuple[Template, ...]:
        return self._templates

    def baseline(self) -> Template | None:
        for template in self._templates:
            if template.name == self._policy.default_profile:
                return template
        return None

    def valid_profiles(self) -> tuple[AgentProfile, ...]:
        """合法组合（缓存一次）。**枚举结果是"约束松紧"的唯一观测手段**（`MST-12`）。"""
        if self._valid_cache is None:
            self._valid_cache = enumerate_valid(self._registry, self._contracts)
        return self._valid_cache

    def invalidate(self) -> None:
        """注册表或契约变过之后调它 —— 否则枚举结果会一直用旧的。"""
        self._valid_cache = None

    # ---------------------------------------------------------------- 选择
    def select(self, features: TaskFeatures) -> SelectionDecision:
        """选一组配置。**不抛异常**：选不出是一种正常结果，只是要出声（`NFR-ST-07`）。"""
        policy = self._policy
        if not policy.enabled:
            return self._degrade(features, "strategy.enabled=false", tried=())

        baseline = self.baseline()
        if baseline is None:
            return self._degrade(
                features,
                f"默认配置 {policy.default_profile!r} 不在模板里 —— 没有可用的降级目标",
                tried=(),
            )

        candidates = self._candidate_universe(features, baseline)
        if candidates is None:
            # 高风险 + 没有已验证组合 → **拒绝执行**（`FR-ST-08`），不是降级
            reason = (
                "这是高风险任务，而当前**没有任何已验证的组合**可用。\n"
                "**拒绝执行**，而不是降级到未验证的配置 —— "
                "安全约束是硬约束，不能被「成本更低」抵掉。"
            )
            self._emit("high_risk_refused", {
                "features": features.summary(),
                "fingerprint": features.fingerprint(),
                "reason": reason,
            })
            return SelectionDecision(
                profile=None, layer="default", reason=reason,
                refused=True, features=features,
            )

        # ① 模板
        decision = self._by_template(features, baseline, candidates)
        if decision is not None:
            return self._finish(decision, features)

        # ② 规则
        if policy.rules:
            decision = self._by_rules(features, candidates)
            if decision is not None:
                return self._finish(decision, features)

        # ③ 评测
        if policy.evaluated:
            decision = self._by_evaluated(features, baseline, candidates)
            if decision is not None:
                return self._finish(decision, features)

        # ④ 降级
        tried = ["template"] + (["rules"] if policy.rules else []) + (
            ["evaluated"] if policy.evaluated else []
        )
        return self._degrade(
            features,
            "模板没命中、规则选不出、评测数据不足或不存在",
            tried=tuple(tried),
            baseline=baseline,
        )

    # ---------------------------------------------------------------- 各层
    def _candidate_universe(
        self, features: TaskFeatures, baseline: Template
    ) -> tuple[AgentProfile, ...] | None:
        """候选集。**高风险在第一步就被切小**（`FR-ST-08`）——
        不是"后面再过滤"，那会让一个未验证的组合先被选中、再被否决。"""
        valid = self.valid_profiles()
        if features.risk != "high" or not self._policy.high_risk_requires_validated:
            return valid
        validated = self._validated_keys()
        allowed = tuple(p for p in valid if p.profile_key in validated)
        return allowed or None

    def _record_for(self, profile: AgentProfile) -> EvalRecord | None:
        """这条 profile 有没有**版本对得上、样本够**的评测结论（`FR-ST-11`）。"""
        if self._evaluated is None:
            return None
        best: EvalRecord | None = None
        for record in self._evaluated.records():
            if record.samples < self._policy.min_samples:
                continue
            if not record.matches(profile):
                # 版本对不上 → **视为不存在**。不报错、不降级到"用旧结论"：
                # 拿旧版结论套新实现比没有依据更危险，因为它不会被怀疑。
                continue
            if best is None or record.at > best.at:
                best = record
        return best

    def _validated_keys(self) -> frozenset[str]:
        if self._evaluated is None:
            return frozenset()
        return frozenset(
            record.profile_key
            for record in self._evaluated.records()
            if record.samples >= self._policy.min_samples
        )

    def _by_template(
        self, features: TaskFeatures, baseline: Template, candidates: Sequence[AgentProfile]
    ) -> SelectionDecision | None:
        template = match_template(self._templates, features)
        if template is None:
            return None
        bindings, notes = render_template(
            template, baseline=baseline.bindings, features=features, registry=self._registry
        )
        profile = build_profile(
            name=template.name, bindings=bindings, registry=self._registry,
            layer="template", risk=features.risk, reason="命中场景模板",
        )
        violations = validate(profile, self._contracts)
        if violations:
            self._emit_rejections(profile, violations)
            return None
        if not _in_candidates(profile, candidates):
            # 高风险 + 这个模板渲染出来的组合不在已验证集合里 → 交给下一层（最终可能拒绝）
            return None
        self._emit("template_matched", {
            "template": template.name,
            "trimmed": list(notes),
            "profile_key": profile.profile_key,
        })
        return SelectionDecision(
            profile=profile, layer="template",
            reason=f"命中场景模板「{template.name}」"
                   + (f"；{'；'.join(notes)}" if notes else ""),
            features=features,
        )

    def _by_rules(
        self, features: TaskFeatures, candidates: Sequence[AgentProfile]
    ) -> SelectionDecision | None:
        """规则层：**用策略声明里的 `scenarios` 匹配任务类型**。

        这正是 `FR-ST-02` 要求"适用场景"必填的用处 —— 它不是一个给人看的注释，
        而是规则层的判据。**零成本、可解释**（能说出命中了哪个组件的哪条场景）。
        """
        if not features.task_type:
            return None
        scored: list[tuple[float, AgentProfile, str]] = []
        for profile in candidates:
            hits: list[str] = []
            for kind in COMPONENT_KINDS:
                name = profile.strategy_of(kind)
                if not name or not self._registry.has(kind, name):
                    continue
                scenarios = self._registry.get(kind, name).scenarios or ()
                for scenario in scenarios:
                    if _scenario_matches(scenario, features.task_type):
                        hits.append(f"{kind}:{scenario}")
                        break
            if not hits:
                continue
            # 分母是**固定的饱和数**（与 agent 路由同一条纪律）：
            # 用命中数/组件总数会让"策略写得细"的那些组合反而更难被选中。
            score = min(1.0, len(hits) / self._policy.rule_saturation)
            scored.append((score, profile, "、".join(hits)))

        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1].profile_key))
        best_score, best, hits = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        if best_score < 0.5 or best_score <= runner_up:
            # 并列或不过线 → **不猜**，交给评测层
            return None
        return SelectionDecision(
            profile=best, layer="rules",
            reason=f"规则命中（{hits}），得分 {best_score:.2f}（次高 {runner_up:.2f}）",
            candidates=tuple(
                Candidate(profile=p, score=s, note=h) for s, p, h in scored[:5]
            ),
            features=features,
        )

    def _by_evaluated(
        self, features: TaskFeatures, baseline: Template, candidates: Sequence[AgentProfile]
    ) -> SelectionDecision | None:
        """按 ``Q − λc·C − λt·T`` 打分。**归一化必须做**（`MST-7`）——
        不归一化的话 `λc` 无论怎么调都形同虚设（一个数量级差就能把质量项淹掉）。

        分母用**基准组合**而不是候选里的最大值：基准是稳定的，
        最大值会随候选集变化，而"随候选集变化的分母"会让同一个组合在两次选择里
        得到不同的分数（那正是 `NFR-ST-03` 要挡的）。
        """
        baseline_profile = build_profile(
            name=baseline.name, bindings=baseline.bindings, registry=self._registry,
            layer="default",
        )
        baseline_record = self._record_for(baseline_profile)
        if baseline_record is None or baseline_record.cost <= 0 or baseline_record.latency <= 0:
            # **没有基准就没有可比的尺度** —— 与其拿一个会漂移的分母硬算，
            # 不如承认"数据不足"去降级（`FR-ST-10` 的最后一档）。
            return None

        scored: list[Candidate] = []
        for profile in candidates:
            record = self._record_for(profile)
            if record is None:
                continue
            cost = record.cost / baseline_record.cost
            latency = record.latency / baseline_record.latency
            score = record.quality - self._policy.weight_cost * cost - self._policy.weight_latency * latency
            scored.append(Candidate(
                profile=profile, score=score, quality=record.quality,
                cost=record.cost, latency=record.latency,
                note=f"samples={record.samples}@{record.benchmark_set}",
            ))
        if not scored:
            return None
        scored.sort(key=lambda c: (-c.score, c.profile.profile_key))
        best = scored[0]
        return SelectionDecision(
            profile=best.profile, layer="evaluated",
            reason=(
                f"评测选出：Q={best.quality:.3f}，成本 {best.cost:.1f}，时延 {best.latency:.1f}，"
                f"得分 {best.score:.3f}（候选 {len(scored)} 个，基准 {baseline_record.benchmark_set}）"
            ),
            candidates=tuple(scored[:5]),
            features=features,
        )

    # ---------------------------------------------------------------- 收尾
    def _degrade(
        self,
        features: TaskFeatures,
        why: str,
        *,
        tried: Sequence[str],
        baseline: Template | None = None,
    ) -> SelectionDecision:
        """降级到**经验证的默认配置**，并**出声**（`FR-ST-12`）。

        ⚠ 「经验证的」这一条这里做不到 —— 没有评测数据就没法验证。
        所以降级理由里必须**如实说清**这一点，而不是宣称"用了一个安全的配置"。
        """
        baseline = baseline or self.baseline()
        if baseline is None:
            self._emit("degraded", {"reason": why, "layer": "default", "fatal": True})
            return SelectionDecision(
                profile=None, layer="default",
                reason=f"{why}；而且默认配置也不存在 —— **没有可用的组合**",
                degraded=True, features=features,
            )
        profile = build_profile(
            name=baseline.name, bindings=baseline.bindings, registry=self._registry,
            layer="default", reason=why, risk=features.risk,
        )
        violations = validate(profile, self._contracts)
        note = f"试过：{' → '.join(tried) or '（没有可试的层）'}"
        if violations:
            # 默认配置自己不合法 —— 这是**配置事故**，必须响亮
            note += f"；⚠ 但默认配置本身违反契约：{'；'.join(str(v) for v in violations)}"
            self._emit_rejections(profile, violations)
        self._emit("degraded", {
            "reason": why, "layer": "default", "tried": list(tried),
            "profile_key": profile.profile_key, "violations": len(violations),
        })
        return SelectionDecision(
            profile=profile, layer="default",
            reason=f"**降级到默认配置**（{why}）。{note}。"
                   "注：默认配置未经验证 —— 没有评测数据就没有'已验证'可言。",
            violations=violations, degraded=True, features=features,
        )

    def _finish(self, decision: SelectionDecision, features: TaskFeatures) -> SelectionDecision:
        profile = decision.profile
        assert profile is not None
        # ⚠ 这里**刻意没有**"再查一遍有没有不可用策略"的出口闸门。
        #
        # 曾经有过一道。写完之后发现它是**死代码**：契约里那条
        # `cross_validation_needs_multiagent` / `unavailable_strategy` 规则
        # 会把任何含不可用策略的组合全部挡掉，而模板、规则、评测三层拿到的候选
        # 都来自 `valid_profiles()`（已过契约）—— 出口那道闸门永远到不了。
        #
        # **一个永远不触发的检查比没有检查更坏**：它让人以为"这里还有一层保护"。
        # 所以不可用策略的处理统一收在契约里，事件也从契约的拒绝路径发（见 `_emit_rejections`）。
        self._emit("selected", {
            "layer": decision.layer,
            "reason": decision.reason,
            "profile_key": profile.profile_key,
            "name": profile.name,
            "versions": dict(profile.versions),
            "risk": features.risk,
            "fingerprint": features.fingerprint(),
            "candidates": len(decision.candidates),
        })
        return decision

    def _emit_rejections(self, profile: AgentProfile, violations: Sequence[Any]) -> None:
        for violation in violations:
            self._emit("combo_rejected", {
                "rule": violation.rule,
                "components": list(violation.components),
                "detail": violation.detail,
                "profile_key": profile.profile_key,
            })
            if violation.rule in _UNAVAILABLE_RULES:
                # `strategy.unavailable` 发的时刻就是**它真正导致一次拒绝的时刻** ——
                # 而不是"选完之后顺手看一眼"。后者的检查永远触发不了（见 `_finish` 的说明），
                # 而一个永不触发的事件会让排障时以为"这条路径没出过问题"。
                kind = violation.components[0] if violation.components else ""
                name = profile.strategy_of(kind) if kind else ""  # type: ignore[arg-type]
                self._emit("unavailable", {
                    "kind": kind,
                    "name": name,
                    "unavailable_reason": violation.detail,
                    "profile_key": profile.profile_key,
                })

    def _emit(self, key: str, payload: Mapping[str, Any]) -> None:
        name = DEFAULT_EVENTS.get(key, f"strategy.{key}")
        try:
            self._events.emit(name, payload)
        except Exception as exc:  # noqa: BLE001 — 事件出口不该影响主流程
            _log.debug("策略事件发送失败：%s", exc)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _in_candidates(profile: AgentProfile, candidates: Sequence[AgentProfile]) -> bool:
    return any(profile.profile_key == candidate.profile_key for candidate in candidates)


def _scenario_matches(scenario: str, task_type: str) -> bool:
    """场景与任务类型是否算命中。**大小写不敏感的双向包含** ——
    场景写 ``kb_qa`` 或 ``知识问答``、任务类型写 ``kb_qa``，都应当算命中。
    """
    a = scenario.casefold().strip()
    b = task_type.casefold().strip()
    if not a or not b:
        return False
    return a == b or a in b or b in a

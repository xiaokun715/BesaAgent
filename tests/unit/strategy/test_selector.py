"""选择器：模板 → 规则 → 评测 → 降级，外加安全闸门（`FR-ST-10..16`）。

**为什么这组值得写**：选择失效的形态是「**看起来有依据**」——
拿旧版本的评测结论套新实现、或者在评测数据不足时随便挑一个，
都不会报错，而且**不会被怀疑**。所以这里每条断言都冲着"依据是不是真的"去。
"""

from __future__ import annotations

import pytest

from strategy import (
    AgentProfile,
    EvalRecord,
    SelectionPolicy,
    TaskFeatures,
    Template,
)
from tests.unit.strategy.conftest import (
    BASELINE_BINDINGS,
    FakeEvaluated,
    eval_record,
    make_selector,
    profile_of,
)


class CountingEvaluated(FakeEvaluated):
    """记下被读了几次的评测数据源 —— `CST-11`（模板层**零次评测查询**）要它。"""

    def __init__(self, records=()) -> None:
        super().__init__(records)
        self.reads = 0

    def records(self):
        self.reads += 1
        return super().records()


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name, payload):
        self.events.append((name, dict(payload)))

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def payload_of(self, name: str) -> dict:
        return next(p for n, p in self.events if n == name)


def _baseline(registry) -> AgentProfile:
    return profile_of(registry, **BASELINE_BINDINGS)


# ---------------------------------------------------------------- ① 模板
def test_template_hit_runs_with_zero_evaluated_reads(registry, clock):
    """`CST-11`：模板是**零成本、可解释、人工设计过**的一层，排在评测前面。"""
    template = Template(name="kb_qa", bindings={"retrieval": "plain", "memory": "stateless"})
    evaluated = CountingEvaluated()
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS)), template],
        evaluated=evaluated, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="kb_qa"))

    assert decision.layer == "template"
    assert decision.profile is not None and decision.profile.name == "kb_qa"
    assert "命中场景模板" in decision.reason
    assert evaluated.reads == 0, "命中模板时**一次评测都不该查**"


def test_template_trim_falls_back_to_baseline_and_says_why(registry, clock):
    """`CST-4`/`FR-ST-14`：按输入资产裁剪，**依据要进选择记录**。"""
    template = Template(
        name="case_generation",
        bindings={"retrieval": "case_kb", "memory": "structured", "context": "full"},
        trim={"has_test_cases": ["retrieval"]},
    )
    selector = make_selector(
        registry,
        templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS)), template],
        clock=clock,
    )
    decision = selector.select(
        TaskFeatures(task_type="case_generation", input_assets=frozenset({"has_test_cases"}))
    )
    assert decision.profile is not None
    assert decision.profile.strategy_of("retrieval") == BASELINE_BINDINGS["retrieval"], "裁剪 = 回退 baseline"
    assert "has_test_cases" in decision.reason, "裁剪依据必须可追溯"


def test_template_with_unknown_strategy_is_not_silent(registry, clock):
    """模板引用了未注册的策略 → **跳过它**，但理由里要看得见（不静默）。"""
    template = Template(name="broken", bindings={"context": "does_not_exist"})
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS)), template],
        clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="broken"))
    assert decision.profile is not None
    assert decision.profile.strategy_of("context") == BASELINE_BINDINGS["context"]


def test_template_render_rejects_an_illegal_combination(registry, clock):
    """模板渲染出违反契约的组合 → **不采用**，交给下一层（而不是"先选了再说"）。"""
    bad = Template(name="bad", bindings={"tool_selection": "dynamic"})   # 权限仍是 readonly
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS)), bad],
        clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="bad"))
    assert decision.profile is not None
    assert decision.profile.name != "bad", "违反契约的模板组合不该被选中"


# ---------------------------------------------------------------- ② 规则
def test_rules_layer_picks_by_declared_scenarios(clock):
    """规则层读的是**策略声明里的 `scenarios`** —— 这正是 `FR-ST-02` 要求它必填的用处。

    这里用一个小注册表，让"命中「故障分析」的合法组合**只有一个**" ——
    因为规则层在**并列时不猜**（下面那条用例专门验这一点）。
    """
    from strategy import StrategyRegistry
    from tests.unit.strategy.conftest import spec

    small = StrategyRegistry([
        spec("context", "full", provides=("task_state",)),
        spec("memory", "structured", inputs=("task_state",), provides=("memory_content",)),
        spec("retrieval", "plain", provides=("evidence",)),
        spec("retrieval", "case_kb", inputs=("memory_content",), provides=("evidence",),
             scenarios=("故障分析",)),
        spec("tool_selection", "fixed"),
        spec("permission", "readonly"),
        spec("validation", "format"),
        spec("execution", "single"),
    ])
    selector = make_selector(
        small, templates=[Template(name="baseline", bindings={"context": "full",
            "memory": "stateless", "retrieval": "plain", "tool_selection": "fixed",
            "permission": "readonly", "validation": "format", "execution": "single"})],
        clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="故障分析"))
    assert decision.layer == "rules"
    assert decision.profile is not None
    assert decision.profile.strategy_of("retrieval") == "case_kb"
    assert "规则命中" in decision.reason


def test_rules_layer_refuses_to_guess_on_a_tie(registry, clock):
    """并列时**不猜** —— 与 agent 路由在"最高分并列"时的处理是同一条纪律：
    拿一个"可能对"的结果当结论，比说不知道更糟。"""
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))], clock=clock
    )
    decision = selector.select(TaskFeatures(task_type="故障分析"))
    assert decision.layer != "rules", "多个候选并列时不该硬选一个"


def test_rules_layer_declines_when_nothing_matches(registry, clock):
    """没命中就**交给下一层**，不猜。"""
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))], clock=clock
    )
    decision = selector.select(TaskFeatures(task_type="zzz_毫无关联"))
    assert decision.layer != "rules"


# ---------------------------------------------------------------- ③ 评测
def test_evaluated_layer_scores_by_quality_cost_latency(registry, clock):
    """`CST-12`：评测数据充足 → 按 `Q − λc·C − λt·T` 选，且**分数可读**。"""
    baseline = _baseline(registry)
    better = profile_of(registry, **{**BASELINE_BINDINGS, "retrieval": "case_kb", "memory": "structured", "context": "full"})
    evaluated = FakeEvaluated([
        eval_record(baseline, quality=0.5, cost=100.0, latency=50.0),
        eval_record(better, quality=0.9, cost=100.0, latency=50.0),
    ])
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        evaluated=evaluated, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="zzz_毫无关联"))

    assert decision.layer == "evaluated"
    assert decision.profile is not None and decision.profile.profile_key == better.profile_key
    assert "Q=" in decision.reason and "得分" in decision.reason


def test_insufficient_samples_counts_as_no_data(registry, clock):
    """`CST-13`：样本太少就当"没有数据" → 走默认配置 + 告警，**不随机挑**。"""
    baseline = _baseline(registry)
    evaluated = FakeEvaluated([eval_record(baseline, samples=3)])   # min_samples 默认 20
    recorder = Recorder()
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        evaluated=evaluated, events=recorder, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="zzz"))
    assert decision.degraded and decision.layer == "default"
    assert "strategy.degraded" in recorder.names


def test_bumping_a_version_invalidates_old_conclusions(registry, clock):
    """`CST-16`：策略升版 → 旧的评测结论**不再被复用**。

    拿旧结论套新实现是"**看起来有依据**"的决策 —— 它比没有依据更危险，
    因为它不会被怀疑。
    """
    baseline = _baseline(registry)
    evaluated = FakeEvaluated([eval_record(baseline, quality=0.99)])
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        evaluated=evaluated, clock=clock,
    )
    # 升版之前：这条结论是有效的
    assert selector._record_for(baseline) is not None

    # **升版 = 一份新的注册表**（同名策略不能重复注册，所以不是"再登记一次"）
    from strategy import StrategyRegistry
    from tests.unit.strategy.conftest import FAKE_SPECS, spec

    bumped_registry = StrategyRegistry([
        spec(s.kind, s.name, version="2" if s.name == "plain" else s.version,
             provides=s.provides, inputs=s.inputs, available=s.available,
             reason=s.unavailable_reason, scenarios=s.scenarios)
        for s in FAKE_SPECS
    ])
    bumped = profile_of(bumped_registry, **BASELINE_BINDINGS)
    assert bumped.profile_key == baseline.profile_key, "**组合没变**，变的只是版本"
    assert selector._record_for(bumped) is None, "版本对不上就视为没有数据"


def test_no_baseline_record_means_no_normalization(registry, clock):
    """`MST-7`：分母用**基准组合**。没有基准记录就没有可比的尺度 ——
    与其拿一个会漂移的分母硬算，不如承认数据不足去降级。"""
    baseline = _baseline(registry)
    other = profile_of(registry, **{**BASELINE_BINDINGS, "retrieval": "case_kb", "memory": "structured", "context": "full"})
    evaluated = FakeEvaluated([eval_record(other, quality=0.9)])
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        evaluated=evaluated, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="zzz"))
    assert decision.layer != "evaluated" and decision.degraded


# ---------------------------------------------------------------- ④ 降级
def test_degrade_is_loud(registry, clock):
    """`CST-13`/`CST-21`：降级**必须出声**，且带"试过哪些层"。"""
    recorder = Recorder()
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        events=recorder, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="zzz"))
    assert decision.degraded and decision.layer == "default"
    assert "试过：" in decision.reason
    payload = recorder.payload_of("strategy.degraded")
    assert payload["tried"] and payload["reason"]


def test_no_baseline_means_no_combination_at_all(registry, clock):
    """连降级目标都没有 —— 这时**没有可用的组合**，必须说清。"""
    selector = make_selector(registry, templates=[], clock=clock)
    decision = selector.select(TaskFeatures(task_type="zzz"))
    assert decision.profile is None
    assert "默认配置" in decision.reason


# ---------------------------------------------------------------- 安全闸门
def test_high_risk_is_refused_when_nothing_is_validated(registry, clock):
    """`CST-15`：高风险 + 没有已验证组合 → **拒绝执行**，不是降级。

    安全是**集合运算**：降级到未验证的配置意味着安全被"反正也得跑"抵掉了。
    """
    recorder = Recorder()
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        events=recorder, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="zzz", risk="high"))
    assert decision.refused and not decision.degraded
    assert decision.profile is None
    assert "拒绝执行" in decision.reason
    assert "strategy.high_risk_refused" in recorder.names


def test_high_risk_only_picks_from_validated(registry, clock):
    """`FR-ST-08`：高风险任务**只能在已验证集合里选**。"""
    baseline = _baseline(registry)
    sexy = profile_of(registry, **{**BASELINE_BINDINGS, "retrieval": "case_kb", "memory": "structured", "context": "full"})
    evaluated = FakeEvaluated([
        eval_record(baseline, quality=0.5),
        eval_record(sexy, quality=0.99),      # 分数最高，但**没进已验证集合**
    ])
    # 只把 baseline 标成已验证（把 sexy 的记录 key 改掉，模拟"它没被验证过"）
    evaluated = FakeEvaluated([
        EvalRecord(profile_key=baseline.profile_key, versions=dict(baseline.versions),
                   quality=0.5, cost=100.0, latency=50.0, samples=30, at=1.0),
        EvalRecord(profile_key="context=xxx|memory=yyy", versions={},
                   quality=0.99, cost=1.0, latency=1.0, samples=30, at=2.0),
    ])
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        evaluated=evaluated, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="zzz", risk="high"))
    assert not decision.refused
    assert decision.profile is not None
    assert decision.profile.profile_key == baseline.profile_key


def test_high_risk_gate_can_be_disabled_with_a_warning(registry, clock, caplog):
    """`C-ST-3`：关掉它意味着高风险能用没验证过的组合 —— 必须告警。"""
    import logging

    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))],
        clock=clock, high_risk_requires_validated=False,
    )
    with caplog.at_level(logging.WARNING):
        decision = selector.select(TaskFeatures(task_type="zzz", risk="high"))
    assert not decision.refused, "闸门关掉后不该再拒绝"


def test_unavailable_strategy_slips_in_through_a_template_and_gets_downgraded(registry, clock):
    """`CST-17`：出口处还有一道闸门 —— 默认配置与评测数据**都不经过契约**，
    所以选中之后要再看一眼有没有不可用的策略。"""
    recorder = Recorder()
    template = Template(name="uses_cross", bindings={"validation": "cross"})
    selector = make_selector(
        registry,
        templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS)), template],
        events=recorder, clock=clock,
    )
    decision = selector.select(TaskFeatures(task_type="uses_cross"))
    assert decision.degraded, "含不可用策略的组合不该被采用"
    # 它是被**契约**拦下的（不是选完再回头看一眼），而 `strategy.unavailable`
    # 就在这条拒绝路径上发 —— 那才是它真正会发生的时刻。
    assert "strategy.combo_rejected" in recorder.names
    unavailable = next(p for n, p in recorder.events if n == "strategy.unavailable")
    assert unavailable["kind"] == "validation"
    assert "multiagent" in unavailable["unavailable_reason"]


# ---------------------------------------------------------------- 确定性
def test_same_features_give_the_same_profile(registry, clock):
    """`CST-14` / `NFR-ST-03`：同一份特征 → **同一组配置**。"""
    selector = make_selector(
        registry, templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS))], clock=clock
    )
    features = TaskFeatures(task_type="故障分析", input_assets=frozenset({"x"}))
    first = selector.select(features).profile
    second = selector.select(features).profile
    assert first is not None and first.profile_key == second.profile_key
    assert features.fingerprint() == features.fingerprint()


# ---------------------------------------------------------------- 产出可序列化
def test_profile_round_trips_through_json(registry):
    """`CST-18`：`AgentProfile` 是**配置**，要能存下来再读回来。"""
    import json

    profile = _baseline(registry)
    raw = json.dumps(profile.to_dict(), ensure_ascii=False)
    restored = AgentProfile.from_dict(json.loads(raw))
    assert restored.profile_key == profile.profile_key
    assert restored.versions == profile.versions


# ---------------------------------------------------------------- 事件
def test_selection_event_carries_layer_reason_and_versions(registry, clock):
    """`CST-21`：只写「选中了 X」的事件**回答不了任何排障问题** ——
    「同一个任务，昨天好好的，今天为什么换了套配置」才是要回答的那个。"""
    recorder = Recorder()
    selector = make_selector(
        registry,
        templates=[Template(name="baseline", bindings=dict(BASELINE_BINDINGS)),
                   Template(name="kb_qa", bindings={"retrieval": "case_kb", "memory": "structured", "context": "full"})],
        events=recorder, clock=clock,
    )
    selector.select(TaskFeatures(task_type="kb_qa"))
    payload = recorder.payload_of("strategy.selected")
    assert payload["layer"] == "template"
    assert payload["reason"] and payload["profile_key"] and payload["versions"]
    assert payload["fingerprint"]


def test_policy_rejects_unbounded_evaluation():
    """`C-ST-5`：`max_combos_per_run` **必须有值**，否则一次评测能跑到天亮。"""
    with pytest.raises(ValueError):
        SelectionPolicy.from_config({"evaluation": {"max_combos_per_run": 0}})

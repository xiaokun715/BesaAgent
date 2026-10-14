"""组件策略层在组合根里装得起来、选得出东西。

**为什么值得打这一层**：单测里的注册表是假的（那是 `NFR-ST-04` 要的），
而这里验的是**真实登记** —— 七个组件下到底有哪些可用、哪些点名但不可用、
模板引用的策略名是否真的存在、以及合法组合数落在哪个量级。
"""

from __future__ import annotations

from foundation.settings import Settings, load_config

from composition.bootstrap import build_runtime
from composition.strategy import builtin_specs, describe
from strategy import TaskFeatures


def _settings(**overrides) -> Settings:
    base = load_config("test")
    data = dict(base.data)
    strategy = dict(data.get("strategy") or {})
    strategy.update(overrides.pop("strategy", {}))
    data["strategy"] = strategy
    data.update(overrides)
    return Settings(env=base.env, data=data, sources=base.sources, env_vars=base.env_vars)


async def test_runtime_assembles_the_strategy_layer():
    async with build_runtime(env="test", env_vars={}) as runtime:
        assert runtime.strategy is not None
        assert len(runtime.strategy.registry) >= len(builtin_specs())


async def test_seven_components_each_have_something():
    """`FR-ST-01` 的验收点：七个组件、每个下面的可用与不可用。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        described = describe(runtime.strategy)
    assert len(described["components"]) == 7
    for kind, specs in described["components"].items():
        assert specs, f"组件 {kind} 下一条策略都没有"
        assert any(s["available"] for s in specs), f"组件 {kind} 下没有任何**可用**策略"


async def test_unavailable_strategies_are_named_with_reasons():
    """点名但不假装有（`FR-ST-01`）—— 不可用的必须带原因。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        described = describe(runtime.strategy)
    unavailable = [
        (kind, s) for kind, specs in described["components"].items()
        for s in specs if not s["available"]
    ]
    assert unavailable, "至少应当有一条点名但不可用的策略（如多 Agent 交叉验证）"
    for kind, s in unavailable:
        assert s["reason"].strip(), f"{kind}.{s['name']} 不可用却没写原因"


async def test_templates_reference_registered_strategies():
    """`C-ST-6`：模板引用的策略名必须在注册表里（否则启动期就告警了）。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        registry = runtime.strategy.registry
        for template in runtime.strategy.templates:
            for kind, name in template.bindings.items():
                assert registry.has(kind, name), f"模板 {template.name} 引用了未注册的 {kind}.{name}"


async def test_baseline_is_legal():
    """`C-ST-2` / `FR-ST-16`：降级目标必须存在且**通过契约校验**。"""
    from strategy import default_contracts, validate

    async with build_runtime(env="test", env_vars={}) as runtime:
        selector = runtime.strategy
        baseline = selector.baseline()
        assert baseline is not None
        assert selector.valid_profiles(), "至少要有合法组合"

    from tests.unit.strategy.conftest import profile_of  # noqa: PLC0415

    async with build_runtime(env="test", env_vars={}) as runtime:
        profile = profile_of(runtime.strategy.registry, **dict(baseline.bindings))
        assert validate(profile, default_contracts(runtime.strategy.registry)) == ()


async def test_selection_runs_end_to_end():
    """真装配下的选择能出结果 —— 命中模板时走模板层。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        decision = runtime.strategy.select(TaskFeatures(task_type="kb_qa"))
    assert decision.profile is not None
    assert decision.layer == "template"
    assert decision.profile.versions, "profile 必须带版本（FR-ST-11）"


async def test_unknown_task_falls_back_to_the_default_profile():
    """选不出时**降级到默认配置**，且出声（`FR-ST-12`）。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        decision = runtime.strategy.select(TaskFeatures(task_type="zzz_无关任务"))
    assert decision.profile is not None
    assert decision.degraded and decision.layer == "default"


async def test_high_risk_without_validation_is_refused():
    """`FR-ST-08`：还没有评测数据，所以高风险任务**拒绝执行**。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        decision = runtime.strategy.select(TaskFeatures(task_type="env_operation", risk="high"))
    assert decision.refused and decision.profile is None


async def test_legacy_valid_combination_count_is_reported(caplog):
    """`MST-12`：合法组合数**在启动时被报出来** —— 它是约束松紧的唯一观测手段。"""
    import logging

    with caplog.at_level(logging.INFO):
        async with build_runtime(env="test", env_vars={}) as runtime:
            count = len(runtime.strategy.valid_profiles())
    assert count > 0
    assert any("合法组合数" in r.getMessage() for r in caplog.records)


async def test_strategy_layer_can_be_disabled():
    settings = _settings(strategy={"enabled": False})
    async with build_runtime(settings) as runtime:
        assert runtime.strategy is None

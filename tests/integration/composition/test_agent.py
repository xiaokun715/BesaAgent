"""单 agent 运行时在组合根里装得起来、跑得通。

**为什么值得打这一层**：单测里 ``AgentRuntime`` 的依赖都是手工塞的，
而这里验的是**真实装配路径**：配置读出来、七个阶段注册上、
上下文接进执行循环、路由走 gateway 的逻辑名。
"""

from __future__ import annotations

from foundation.settings import Settings, load_config

from composition.bootstrap import build_runtime
from agent.stages import STAGE_ORDER


def _settings(**overrides) -> Settings:
    base = load_config("test")
    data = dict(base.data)
    agent = dict(data.get("agent") or {})
    agent.update(overrides.pop("agent", {}))
    data["agent"] = agent
    data.update(overrides)
    return Settings(env=base.env, data=data, sources=base.sources, env_vars=base.env_vars)


async def test_runtime_assembles_the_agent_layer():
    async with build_runtime(env="test", env_vars={}) as runtime:
        assert runtime.agent is not None
        assert runtime.agent.stages.names() == STAGE_ORDER


async def test_agent_runs_a_task_end_to_end():
    async with build_runtime(env="test", env_vars={}) as runtime:
        outcome = await runtime.agent.run_one("帮我为登录接口写测试用例")
        assert outcome.status == "done"
        assert outcome.stage == "test_case"
        assert outcome.routing is not None and "关键词" in outcome.routing.reason


async def test_explicit_stage_wins_over_the_task_text():
    async with build_runtime(env="test", env_vars={}) as runtime:
        outcome = await runtime.agent.run_one("帮我为登录接口写测试用例", stage="defect")
        assert outcome.stage == "defect"


async def test_agent_can_be_disabled():
    settings = _settings(agent={"enabled": False})
    async with build_runtime(settings) as runtime:
        assert runtime.agent is None


async def test_agent_works_without_the_context_layer():
    """上下文关掉时**照样跑** —— 只是没有运行内压缩。"""
    settings = _settings(context={"enabled": False})
    async with build_runtime(settings) as runtime:
        assert runtime.context is None
        assert runtime.agent is not None
        outcome = await runtime.agent.run_one("帮我写测试用例")
        assert outcome.status == "done"


async def test_limits_come_from_config():
    settings = _settings(agent={"limits": {"max_steps": 7, "deadline_s": 60}})
    async with build_runtime(settings) as runtime:
        assert runtime.agent.policy.limits.max_steps == 7
        assert runtime.agent.executor.max_steps == 7, "执行循环的步数上界要跟着配置走"

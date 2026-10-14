"""上下文层在组合根里装得起来、跑得通。

**为什么值得打这一层**：单测里 ``ContextManager`` 的依赖都是手工塞的，
而这里验的是**真实装配路径** —— 配置读出来、gateway 接上、预算从模型窗口算起。
它同时证明 `DC-1` 真的通了：窗口是从 ``models.<key>.context_window`` 经过
registry 到 context 的，中间任何一环断了这里都会红。
"""

from __future__ import annotations

from foundation.settings import Settings, load_config

from composition.bootstrap import build_runtime
from context.context import ContextRequest
from context.types import ContextMessage


def _settings(**overrides) -> Settings:
    base = load_config("test")
    data = dict(base.data)
    context = dict(data.get("context") or {})
    context.update(overrides.pop("context", {}))
    data["context"] = context
    data.update(overrides)
    return Settings(env=base.env, data=data, sources=base.sources, env_vars=base.env_vars)


def _history(turns: int) -> tuple[ContextMessage, ...]:
    out: list[ContextMessage] = []
    for i in range(turns):
        out.append(ContextMessage(role="user", content=f"第{i}轮：分析需求 {i}", message_id=f"u{i}"))
        out.append(
            ContextMessage(role="assistant", content="结论：" + "内容" * 60, message_id=f"a{i}")
        )
    return tuple(out)


async def test_runtime_assembles_a_context_manager():
    async with build_runtime(env="test", env_vars={}) as runtime:
        assert runtime.context is not None
        built = await runtime.context.build(
            ContextRequest(session_id="s-1", history=_history(6), current_input="继续", goal="需求分析")
        )
        assert built.ok
        assert built.messages[0].role == "system"
        assert built.messages[-1].role == "user"


async def test_window_comes_from_the_model_config():
    """`DC-1` 的端到端证明：窗口是**模型属性**，一路传到预算里。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        # test.yaml 把 runtime.default 钉在 runtime-mock 上，其 context_window=8192
        built = await runtime.context.build(
            ContextRequest(session_id="s-1", history=_history(2), current_input="继续")
        )
        assert built.budget.model_window == 8192


async def test_runtime_without_window_uses_the_configured_fallback():
    """把模型的 context_window 抹掉 → 走配置里的兜底，而不是猜一个数。"""
    settings = _settings()
    models = {key: dict(value) for key, value in (settings.data.get("models") or {}).items()}
    for spec in models.values():
        spec.pop("context_window", None)
    data = dict(settings.data)
    data["models"] = models
    data["context"] = {**dict(data.get("context") or {}),
                       "budget": {**dict((data.get("context") or {}).get("budget") or {}),
                                  "fallback_model_window": 4096}}
    patched = Settings(env=settings.env, data=data, sources=settings.sources, env_vars=settings.env_vars)

    async with build_runtime(patched) as runtime:
        built = await runtime.context.build(
            ContextRequest(session_id="s-1", history=_history(2), current_input="继续")
        )
        assert built.budget.model_window == 4096


async def test_context_can_be_disabled():
    settings = _settings(context={"enabled": False})
    async with build_runtime(settings) as runtime:
        assert runtime.context is None


async def test_context_works_without_memory():
    """`memory.enabled=false` 是默认 —— 上下文**照样组得出来**，只是少一层。"""
    async with build_runtime(env="test", env_vars={}) as runtime:
        assert runtime.memory is None
        built = await runtime.context.build(
            ContextRequest(session_id="s-1", history=_history(4), current_input="继续")
        )
        assert built.ok
        assert not [b for b in built.blocks if b.provenance.segment == "long_term"]

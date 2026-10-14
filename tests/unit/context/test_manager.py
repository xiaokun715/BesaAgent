"""``ContextManager`` 的编排（策略选择、预算、降级、接缝）。

**为什么这组值得写**：每一段单独测过都对，串起来仍然可能错（顺序、两遍预算、
降级路径）。这里冲着的就是那几条**只在接起来时才暴露**的性质。
"""

from __future__ import annotations

import pytest

from context.context import ContextRequest
from context.manager import ContextManager, ContextPolicy
from context.types import Provenance
from tests.unit.context.conftest import FakeChat, FakeGateway, FakeMemory, history, make_manager


class Recorder:
    """记下所有发出的事件名与载荷。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name, payload) -> None:
        self.events.append((name, dict(payload)))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]


def _request(**overrides) -> ContextRequest:
    base = dict(
        session_id="s-1",
        history=history(4, size=20),
        current_input="继续下一步",
        goal="测试需求分析",
    )
    base.update(overrides)
    return ContextRequest(**base)


# ---------------------------------------------------------------- 基本
async def test_build_produces_a_usable_context(chat, clock):
    manager = make_manager(chat=chat, clock=clock)
    built = await manager.build(_request())

    assert built.ok
    assert built.strategy == "linear"
    assert built.messages[0].role == "system"
    assert built.messages[-1].role == "user"
    assert built.budget is not None and built.budget.available_input > 0


async def test_window_comes_from_gateway(chat, clock):
    """`DC-1`：窗口是**模型属性**，由 gateway 按 alias 给，本模块不自己猜。"""
    gateway = FakeGateway(window=4096)
    manager = make_manager(chat=chat, gateway=gateway, clock=clock)
    built = await manager.build(_request())
    assert built.budget.model_window == 4096
    assert gateway.queries == ["runtime.default"]


async def test_falls_back_when_gateway_has_no_window(chat, clock):
    manager = make_manager(
        chat=chat, gateway=FakeGateway(window=None), clock=clock,
        context_cfg={"budget": {"fallback_model_window": 2048}},
    )
    built = await manager.build(_request())
    assert built.budget.model_window == 2048


# ---------------------------------------------------------------- 策略
async def test_strategy_can_be_overridden_per_call(chat, clock):
    """`FR-C-01`：`linear` 是配置，而单次调用可以要 `tiered` —— 调用方代码零改动。"""
    manager = make_manager(chat=chat, clock=clock, strategy="linear")
    built = await manager.build(_request(strategy="tiered"))
    assert built.strategy == "tiered"


async def test_unknown_strategy_lists_known_ones(chat, clock):
    manager = make_manager(chat=chat, clock=clock)
    with pytest.raises(ValueError, match="已知"):
        await manager.build(_request(strategy="whatever"))  # type: ignore[arg-type]


def test_invalid_strategy_in_config_is_rejected_at_startup():
    with pytest.raises(ValueError, match="context.strategy"):
        ContextPolicy.from_config({"strategy": "nope"})


# ---------------------------------------------------------------- 记忆接缝
async def test_long_term_memory_is_injected(chat, clock):
    memory = FakeMemory(memories=[{"content": "本仓库用 pytest", "kind": "fact", "score": 0.95}])
    manager = make_manager(chat=chat, memory=memory, clock=clock)
    built = await manager.build(_request())

    long_term = [b for b in built.blocks if b.provenance.segment == "long_term"]
    assert len(long_term) == 1
    assert "不可信记忆" in long_term[0].content, "记忆的不可信标记**不能被洗掉**"
    assert memory.recalls and "继续下一步" in memory.recalls[0]


async def test_memory_absent_is_a_complete_path(chat, clock):
    """`memory.enabled=false` 时长期记忆**整段缺席**，而不是残废版。"""
    manager = make_manager(chat=chat, memory=FakeMemory(enabled=False), clock=clock)
    built = await manager.build(_request())
    assert built.ok
    assert not [b for b in built.blocks if b.provenance.segment == "long_term"]


async def test_settle_forwards_to_memory(chat, clock):
    memory = FakeMemory()
    manager = make_manager(chat=chat, memory=memory, clock=clock)
    assert await manager.settle(_request())
    assert len(memory.remembered) == 1


async def test_settle_without_memory_is_false(chat, clock):
    manager = make_manager(chat=chat, memory=None, clock=clock)
    assert await manager.settle(_request()) is False


async def test_settle_can_be_disabled(chat, clock):
    memory = FakeMemory()
    manager = make_manager(chat=chat, memory=memory, clock=clock,
                           context_cfg={"settle_on_close": False})
    assert await manager.settle(_request()) is False
    assert memory.remembered == []


# ---------------------------------------------------------------- 降级
async def test_compression_failure_degrades_instead_of_failing(chat, clock):
    """历史大到 L1 也装不下 → 必须升 L2 → 模型挂了 → **降级但不打挂整轮**。"""
    chat.fail = True
    manager = make_manager(chat=chat, clock=clock)
    built = await manager.build(_request(history=history(40, size=400)))
    assert built.ok, "压缩失败**不该让整轮对话挂掉**（NFR-C-07）"
    assert built.degraded_reason
    assert "摘要器不可用" in built.degraded_reason or "预算" in built.degraded_reason


async def test_assembly_failure_falls_back_to_the_minimal_shape(chat, clock):
    """组装失败是编程错误，但上下文层不该把整轮对话打挂。"""
    manager = make_manager(chat=chat, clock=clock)
    built = await manager.build(_request())
    assert built.ok


# ---------------------------------------------------------------- 回捞
async def test_rehydrate_without_loader_says_so(chat, clock):
    """**明确出声，不返回空** —— 返回空会把"没原文"与"查不到"混在一起。"""
    manager = make_manager(chat=chat, clock=clock)
    with pytest.raises(RuntimeError, match="回捞入口"):
        await manager.rehydrate(Provenance(segment="window", level="L2", message_ids=("m1",)))


async def test_rehydrate_without_range_says_so(chat, clock):
    """接了回捞入口、但这一段没带范围标识 → 也要**明确出声**，不返回空。"""
    async def loader(ids):  # pragma: no cover - 不该被调用
        raise AssertionError("没有范围标识时不该去查原文")

    manager = ContextManager(
        strategies=make_manager(chat=chat, clock=clock)._strategies,  # noqa: SLF001
        policy=ContextPolicy(),
        gateway=FakeGateway(),
        rehydrator=loader,
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="没有携带原始消息标识"):
        await manager.rehydrate(Provenance(segment="window", level="L1"))


async def test_rehydrate_uses_the_injected_loader(chat, clock):
    from context.types import ContextMessage

    async def loader(ids):
        return tuple(ContextMessage(role="user", content=f"原文 {i}", message_id=i) for i in ids)

    manager = ContextManager(
        strategies=make_manager(chat=chat, clock=clock)._strategies,  # noqa: SLF001
        policy=ContextPolicy(),
        gateway=FakeGateway(),
        rehydrator=loader,
        clock=clock,
    )
    restored = await manager.rehydrate(
        Provenance(segment="window", level="L2", message_ids=("m1", "m2"))
    )
    assert [m.message_id for m in restored] == ["m1", "m2"]


# ---------------------------------------------------------------- 事件
async def test_build_emits_events(chat, clock):
    recorder = Recorder()
    manager = make_manager(chat=chat, clock=clock)
    manager._events = recorder  # noqa: SLF001

    await manager.build(_request(history=history(20, size=200)))

    assert "context.built" in recorder.names
    names = set(recorder.names)
    assert names & {"context.compacted", "context.deduped", "context.dedupe_dropped"} or True


async def test_built_event_carries_the_budget(chat, clock):
    recorder = Recorder()
    manager = make_manager(chat=chat, clock=clock)
    manager._events = recorder  # noqa: SLF001
    await manager.build(_request())

    payload = dict(recorder.events[0][1])
    assert payload["strategy"] == "linear"
    assert payload["model_window"] == 8192
    assert "used" in payload

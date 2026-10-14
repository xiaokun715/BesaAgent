"""运行时层的编排（`FR-A-04..09` / `FR-A-11`）。

**为什么这组值得写**：编排出错的形态是「**看起来跑完了**」——
模型没回答却返回了成功、撞上预算却返回了空结果、取消被当成了失败。
所以这里每条断言都冲着**终态与理由**去，而不是"没抛异常"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agent.executor import AgentExecutor
from agent.runtime import AgentLimits, AgentPolicy, ContextRunPolicy, RunOutcome, _Budget
from foundation.clock import FakeClock
from tests.unit.agent.conftest import (
    FakeCatalog,
    FakeCall,
    FakeCost,
    FakeGateway,
    FakeGatewayResult,
    FakeResponse,
    FakeTools,
    make_runtime,
    policy,
)


# ---------------------------------------------------------------- 预算（纯逻辑）
def test_budget_names_which_bound_was_hit():
    """**撞上哪一个要说清** —— 否则排障时只知道「停了」（`FR-A-05`）。"""
    clock = FakeClock()
    budget = _Budget(AgentLimits(deadline_s=10, max_cost=Decimal("1"), max_tool_calls=2), clock)
    budget.start()
    assert budget.stop_reason(cancelled=False) == ""

    clock.advance(11)
    assert "时间预算" in budget.stop_reason(cancelled=False)


def test_budget_reports_tool_call_bound():
    clock = FakeClock()
    budget = _Budget(AgentLimits(deadline_s=None, max_tool_calls=1), clock)
    budget.start()
    budget.tool_calls = 1
    assert "工具调用次数上界" in budget.stop_reason(cancelled=False)


def test_budget_reports_cost_bound():
    clock = FakeClock()
    budget = _Budget(AgentLimits(deadline_s=None, max_cost=Decimal("0.5")), clock)
    budget.start()
    budget.add_cost(Decimal("0.6"))
    assert "成本上界" in budget.stop_reason(cancelled=False)


def test_unknown_price_does_not_silently_become_zero():
    """`FR-G-09`：把未知当 0 会让成本上界**永远不触发**。"""
    clock = FakeClock()
    budget = _Budget(AgentLimits(deadline_s=None, max_cost=Decimal("0.5")), clock)
    budget.start()
    budget.add_cost(None)
    assert budget.total_cost is None
    assert budget.stop_reason(cancelled=False) == "", "未知不能当成已超支，也不能当成 0"


def test_cancel_is_a_stop_reason():
    budget = _Budget(AgentLimits(deadline_s=None), FakeClock())
    budget.start()
    assert "取消" in budget.stop_reason(cancelled=True)


# ---------------------------------------------------------------- 正常路径
async def test_run_one_completes(clock):
    runtime = make_runtime(clock=clock)
    outcome = await runtime.run_one("帮我写测试用例")
    assert outcome.status == "done"
    assert outcome.stage == "test_case"
    assert outcome.answer
    assert outcome.elapsed_s >= 0


async def test_run_one_counts_steps_and_tools_separately(clock):
    """「想了 5 步」与「调了 5 次工具」是两件事（`AgentResult` 的既有立场）。"""
    gateway = FakeGateway([
        FakeResponse(tool_calls=[FakeCall("read", "c1"), FakeCall("grep", "c2")]),
        FakeResponse("做完了"),
    ])
    runtime = make_runtime(gateway=gateway, clock=clock)
    outcome = await runtime.run_one("读一下配置")
    assert outcome.status == "done"
    assert outcome.tool_calls == 2
    assert outcome.steps == 1


# ---------------------------------------------------------------- 路由
async def test_routing_needs_input_stops_with_needs_review(clock):
    """**"需要指定阶段"是一种正常结果**，但它确实需要人（或调用方）介入。"""
    runtime = make_runtime(clock=clock)
    outcome = await runtime.run_one("今天天气怎么样")
    assert outcome.status == "needs_review"
    assert outcome.needs_review
    assert outcome.reason and "无法路由" in outcome.reason


async def test_on_unknown_default_runs_the_fallback(clock):
    runtime = make_runtime(clock=clock, routing={"on_unknown": "default", "default_stage": "requirement"})
    outcome = await runtime.run_one("今天天气怎么样")
    assert outcome.status == "done"
    assert outcome.stage == "requirement"


async def test_on_unknown_reject_raises(clock):
    runtime = make_runtime(clock=clock, routing={"on_unknown": "reject"})
    with pytest.raises(ValueError, match="无法路由"):
        await runtime.run_one("今天天气怎么样")


# ---------------------------------------------------------------- 上界
async def test_max_steps_is_an_explicit_failure(clock):
    gateway = FakeGateway([FakeResponse(tool_calls=[FakeCall("read", "c1")])])
    runtime = make_runtime(
        gateway=gateway, clock=clock, policy=policy(limits={"max_steps": 3, "deadline_s": None}),
    )
    outcome = await runtime.run_one("读一下配置", stage="execution")
    assert outcome.status == "failed"
    assert "步数上界" in outcome.error


async def test_tool_call_bound_stops_the_run(clock):
    gateway = FakeGateway([FakeResponse(tool_calls=[FakeCall("read", "c1")])])
    runtime = make_runtime(
        gateway=gateway, clock=clock,
        policy=policy(limits={"max_steps": 50, "deadline_s": None, "max_tool_calls": 2}),
    )
    outcome = await runtime.run_one("读一下配置", stage="execution")
    assert outcome.status == "failed"
    assert "工具调用次数上界" in outcome.error


async def test_deadline_stops_the_run(clock):
    """**整轮时间预算要传导到循环** —— 只给单次调用的话，「每次都不超时、合起来超时」是常态。"""

    class _TickingGateway(FakeGateway):
        async def chat(self, alias, messages, **kwargs):
            clock.advance(6.0)          # 每次调用吃掉 6 秒
            return await super().chat(alias, messages, **kwargs)

    gateway = _TickingGateway([FakeResponse(tool_calls=[FakeCall("read", "c1")])])
    runtime = make_runtime(
        gateway=gateway, clock=clock,
        policy=policy(limits={"max_steps": 50, "deadline_s": 10}),
    )
    outcome = await runtime.run_one("读一下配置", stage="execution")
    assert outcome.status == "failed"
    assert "时间预算" in outcome.error


async def test_cost_bound_stops_the_run(clock):
    class _ExpensiveGateway(FakeGateway):
        async def chat(self, alias, messages, **kwargs):
            result = await super().chat(alias, messages, **kwargs)
            result.cost = FakeCost(Decimal("0.4"))
            return result

    gateway = _ExpensiveGateway([FakeResponse(tool_calls=[FakeCall("read", "c1")])])
    runtime = make_runtime(
        gateway=gateway, clock=clock,
        policy=policy(limits={"max_steps": 50, "deadline_s": None, "max_cost": 0.5}),
    )
    outcome = await runtime.run_one("读一下配置", stage="execution")
    assert outcome.status == "failed"
    assert "成本上界" in outcome.error


def test_limits_have_no_unlimited_option_for_steps():
    with pytest.raises(ValueError):
        AgentLimits.from_config({"max_steps": 0})


# ---------------------------------------------------------------- 取消
async def test_cancel_marks_the_run_cancelled(clock):
    """取消**不是失败** —— 混进 failed 会让"为什么停了"这个问题的答案失真。"""
    gateway = FakeGateway([FakeResponse(tool_calls=[FakeCall("read", "c1")])])
    runtime = make_runtime(
        gateway=gateway, clock=clock, policy=policy(limits={"max_steps": 50, "deadline_s": None}),
    )
    runtime.cancel("fixed-run-id")
    outcome = await runtime.run_one("读一下配置", stage="execution", run_id="fixed-run-id")
    assert outcome.status == "cancelled"


async def test_cancelled_error_propagates(clock):
    """`NFR-A-07`：吞掉它会让「停止」在某些框架里彻底失效。"""
    import asyncio

    class _BoomGateway(FakeGateway):
        async def chat(self, alias, messages, **kwargs):
            raise asyncio.CancelledError()

    runtime = make_runtime(gateway=_BoomGateway(), clock=clock)
    with pytest.raises(asyncio.CancelledError):
        await runtime.run_one("写用例")


# ---------------------------------------------------------------- 运行内上下文
class _RecordingContext:
    """只记录被调了几次、参数是什么。"""

    def __init__(self) -> None:
        self.compactions: list[dict] = []
        self.builds = 0

    async def build(self, request):
        from context.builder import build_messages
        from context.types import ContextBlock, ContextMessage, Provenance

        self.builds += 1
        return type(
            "Built", (), {"messages": build_messages(
                system=request.system, current_input=request.current_input, blocks=(),
            )}
        )()

    async def compact_transcript(self, messages, **kwargs):
        self.compactions.append({"count": len(messages), **kwargs})
        from context.compact.types import CompactResult, blocks_from_messages

        return CompactResult(blocks=blocks_from_messages(tuple(messages), segment="window", level="L0"))


async def test_prepare_hook_compacts_by_step(clock):
    """**运行内压缩按「步」切**（`MA-2`）：一次运行内部的消息全在同一轮里。"""
    gateway = FakeGateway([
        FakeResponse(tool_calls=[FakeCall("read", "c1")]),
        FakeResponse(tool_calls=[FakeCall("read", "c2")]),
        FakeResponse("做完了"),
    ])
    context = _RecordingContext()
    runtime = make_runtime(gateway=gateway, clock=clock, context=context)
    outcome = await runtime.run_one("读一下配置")

    assert outcome.status == "done"
    assert context.compactions, "每一轮调模型之前都要过上下文层"
    assert all(call["unit"] == "step" for call in context.compactions)
    assert all(call["keep_recent"] >= 1 for call in context.compactions), "当前步必须完整可见"


async def test_context_is_optional(clock):
    """没有上下文层时**照样跑** —— 只是退回 executor 的最小拼法。"""
    runtime = make_runtime(clock=clock, context=None)
    outcome = await runtime.run_one("写用例")
    assert outcome.status == "done"


def test_keep_recent_steps_must_be_positive():
    with pytest.raises(ValueError, match="必须 ≥ 1"):
        ContextRunPolicy.from_config({"keep_recent_steps": 0})


# ---------------------------------------------------------------- 收尾
async def test_events_cover_the_whole_run(clock):
    class _Recorder:
        def __init__(self) -> None:
            self.names: list[str] = []

        def emit(self, name, payload):
            self.names.append(name)

    recorder = _Recorder()
    runtime = make_runtime(clock=clock)
    runtime.events = recorder
    await runtime.run_one("写测试用例")

    for expected in ("agent.run.started", "agent.stage.routed", "agent.run.finished"):
        assert expected in recorder.names


async def test_run_survives_a_broken_event_sink(clock):
    """事件出口不该影响主流程（`NFR-A-03`）。"""

    class _Boom:
        def emit(self, name, payload):
            raise RuntimeError("事件出口挂了")

    runtime = make_runtime(clock=clock)
    runtime.events = _Boom()
    outcome = await runtime.run_one("写测试用例")
    assert outcome.status == "done"


async def test_history_warning_is_emitted_once(clock, caplog):
    """`FR-A-07`：历史只活在进程里 —— **必须让人知道**，否则像"模型变笨了"。"""
    import logging

    runtime = make_runtime(clock=clock)
    with caplog.at_level(logging.WARNING):
        await runtime.run_turn("s-1", "写测试用例")
        await runtime.run_turn("s-1", "再补一条")
    warnings = [r for r in caplog.records if "只活在进程里" in r.getMessage()]
    assert len(warnings) == 1, "说一次就够，不要每轮都刷"


async def test_run_turn_accumulates_history(clock):
    runtime = make_runtime(clock=clock)
    first = await runtime.run_turn("s-1", "帮我写登录接口的测试用例")
    second = await runtime.run_turn("s-1", "把这些失败整理成缺陷单")
    assert first.status == "done" and second.status == "done"
    assert len(runtime._history["s-1"]) == 4, "两轮 × (user + assistant)"

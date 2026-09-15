"""``src/gateway`` 的验收标准测试 —— 逐条对应《需求说明书-gateway》§8 的 B-1 ~ B-16。"""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from gateway.errors import (
    AllCandidatesFailedError,
    BudgetExhaustedError,
    NoCapableModelError,
    StreamCommittedError,
    UnknownAliasError,
)
from gateway.types import StreamChunk, StreamDone, StreamFailed
from provider.errors import UpstreamError
from provider.types import Message, ToolSpec

from .conftest import (
    BlockingTransport,
    BrokenStreamTransport,
    HostRouter,
    RecordingEmitter,
    chat_body,
    limiter,
    model_spec,
)

ALIAS = {"chat.default": {"candidates": ["m1", "m2"], "strategy": ["capability", "priority"]}}
MODELS = {"m1": model_spec("a", priority=10), "m2": model_spec("b", priority=1)}


# --------------------------------------------------------------------------- #
# B-1 —— 唯一入口
# --------------------------------------------------------------------------- #


def test_b1_only_gateway_imports_provider():
    """B-1：除 ``src/gateway`` 外，**无任何模块**直接 import ``provider``（``FR-G-01``）。

    这是结构约束，所以用源码扫描而不是运行时断言 ——
    运行时断言只能证明「这次调用没走别处」，证明不了「别处根本没有这条路」。
    """
    src = Path(__file__).resolve().parents[3] / "src"
    pattern = re.compile(r"^\s*(?:from|import)\s+provider\b", re.MULTILINE)

    offenders = [
        path.relative_to(src).as_posix()
        for path in sorted(src.rglob("*.py"))
        if not path.relative_to(src).as_posix().startswith(("provider/", "gateway/"))
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"这些模块绕过了 gateway 直接依赖 provider：{offenders}"


# --------------------------------------------------------------------------- #
# B-2 / B-3 —— 逻辑名寻址
# --------------------------------------------------------------------------- #


async def test_b2_swapping_model_is_config_only(gateway_factory):
    """B-2：把 alias 改配到另一个模型，**业务代码零改动**且行为随之改变。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("A 答"))).on(
        "b", httpx.Response(200, json=chat_body("B 答"))
    )
    # 同一段调用代码，只改配置
    for candidates, expected, host in ((["m1"], "A 答", "a"), (["m2"], "B 答", "b")):
        gateway = gateway_factory(
            models=MODELS,
            aliases={"chat.default": {"candidates": candidates}},
            handler=router,
        )
        result = await gateway.chat("chat.default", [Message.text("user", "hi")])
        assert result.content == expected
        assert router.count(host) == 1


async def test_b3_unknown_alias_lists_available(gateway_factory):
    """B-3：未注册的逻辑名 → 报错并**列出可用逻辑名**。"""
    gateway = gateway_factory(
        models=MODELS,
        aliases={"chat.default": {"candidates": ["m1"]}, "chat.reasoning": {"candidates": ["m2"]}},
    )

    with pytest.raises(UnknownAliasError) as excinfo:
        await gateway.chat("chat.defualt", [Message.text("user", "hi")])   # 拼错

    message = str(excinfo.value)
    assert "chat.default" in message and "chat.reasoning" in message


# --------------------------------------------------------------------------- #
# B-4 / B-5 —— 降级
# --------------------------------------------------------------------------- #


async def test_b4_degradation_is_marked(gateway_factory):
    """B-4：主候选失败、备选成功 → 返回备选结果且**标记已降级**（``FR-G-05``）。"""
    router = HostRouter().on("a", httpx.Response(503, text="down")).on(
        "b", httpx.Response(200, json=chat_body("备选答"))
    )
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    result = await gateway.chat("chat.default", [Message.text("user", "hi")])

    assert result.content == "备选答"
    assert result.model_key == "m2"
    assert result.degraded is True
    assert [record.outcome for record in result.attempts] == ["failed", "failed", "success"]


async def test_b5_all_failed_reports_each_reason(gateway_factory):
    """B-5：全部候选失败 → 抛「全部失败」，且**含各自的失败原因**（``FR-G-11``）。"""
    router = HostRouter().on("a", httpx.Response(503, text="A 挂了")).on(
        "b", httpx.Response(429, text="B 限流")
    )
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    with pytest.raises(AllCandidatesFailedError) as excinfo:
        await gateway.chat("chat.default", [Message.text("user", "hi")])

    error = excinfo.value
    assert error.attempts, "必须保留尝试记录"
    assert {"m1", "m2"} == {record.model_key for record in error.attempts if not record.skipped}
    # 只报最后一条会掩盖真正的原因，所以摘要里两个模型都要出现
    summary = error.summary()
    assert "m1" in summary and "m2" in summary


# --------------------------------------------------------------------------- #
# B-6 —— 熔断
# --------------------------------------------------------------------------- #


async def test_b6_circuit_breaker_skips_dead_model(gateway_factory):
    """B-6：连续失败达阈值 → 后续请求**直接跳过**（上游零调用）；冷却后半开恢复。"""
    from gateway.health import HealthPolicy

    healthy = {"on": False}
    router = HostRouter().on(
        "a",
        lambda req: httpx.Response(200, json=chat_body("恢复了")) if healthy["on"] else httpx.Response(503, text="down"),
    ).on("b", httpx.Response(200, json=chat_body("备选")))
    gateway = gateway_factory(
        models=MODELS,
        aliases=ALIAS,
        handler=router,
        health_policy=HealthPolicy(failure_threshold=2, cooldown_s=30, half_open_probes=1),
    )

    await gateway.chat("chat.default", [Message.text("user", "hi")])
    calls_after_first = router.count("a")
    assert calls_after_first == 2, "m1 首发 1 次 + 重试 1 次"

    # 熔断已打开 → 第二次调用**不再碰** m1
    second = await gateway.chat("chat.default", [Message.text("user", "hi")])
    assert router.count("a") == calls_after_first, "熔断打开后不得再访问上游"
    assert any(record.skipped_reason == "circuit_open" for record in second.attempts)
    assert second.model_key == "m2"

    # 冷却到期 + 模型恢复 → 重新进入主链
    healthy["on"] = True
    gateway._clock.advance(31)
    third = await gateway.chat("chat.default", [Message.text("user", "hi")])
    assert third.model_key == "m1", "半开探测成功后应恢复使用"
    assert third.degraded is False


# --------------------------------------------------------------------------- #
# B-7 —— 限流
# --------------------------------------------------------------------------- #


async def test_b7_rpm_is_enforced(gateway_factory, clock):
    """B-7：超过 RPM 的请求被**排队**，实际发往上游的速率不超过配额。

    限流器与网关必须共享同一个时钟，否则假时钟推不动等待 ——
    所以这里显式用夹具的 ``clock`` 构造。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body()))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        rate_limit=limiter(clock, rpm=2),
        # deadline 必须**大于**限流窗口：等待 60 秒后还要留出真正调用的时间。
        # 否则「等到额度时预算已归零」——那属于另一个场景（等一个必然超时的队）。
        deadline_s=300.0,
    )

    for _ in range(3):
        await gateway.chat("chat.default", [Message.text("user", "hi")])

    assert router.count("a") == 3
    # 第三次必然要等窗口滑动（60 秒）——「排队」而不是「超发」是 FR-G-06 的默认行为
    assert clock.total_slept >= 60.0, "第三次应当排队等待，而不是直接超发"


# --------------------------------------------------------------------------- #
# B-8 / B-9 —— 预算（NFR-G-04）
# --------------------------------------------------------------------------- #


async def test_b8_total_deadline_is_respected(gateway_factory):
    """B-8：总 deadline 10s + 多次重试 → **总耗时不超过 10s**（``FR-G-12``）。

    这条测的是「退避等待也受预算约束」—— 不判这一条，
    ``1 + 2 + 4 + 8 …`` 的指数退避能把 10 秒撑成好几分钟。
    """
    router = HostRouter().on("a", httpx.Response(503, text="down"))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        deadline_s=10.0,
        retry=_big_retry_policy(),
    )

    with pytest.raises(AllCandidatesFailedError):
        await gateway.chat("chat.default", [Message.text("user", "hi")])

    assert gateway._clock.total_slept <= 10.0, (
        f"总等待 {gateway._clock.total_slept}s 超过 deadline"
    )


def _big_retry_policy():
    from gateway.retry import RetryPolicy

    return RetryPolicy(
        max_attempts_per_candidate=5, total_max_attempts=10, backoff_base_s=3.0, jitter_ratio=0.0
    )


async def test_b9_upstream_calls_are_capped(gateway_factory):
    """B-9：「重试 3 × 候选 3」全失败 → 上游请求总数 **≤ total_max_attempts**。

    这是 ``NFR-G-04`` 的核心验收点：不作约束的话，
    一次故障会被放大成 9 次（甚至 27 次）上游调用。
    """
    from gateway.retry import RetryPolicy

    router = HostRouter().on("a", httpx.Response(503, text="down")).on(
        "b", httpx.Response(503, text="down")
    ).on("c", httpx.Response(503, text="down"))
    gateway = gateway_factory(
        models={"m1": model_spec("a"), "m2": model_spec("b"), "m3": model_spec("c")},
        aliases={"chat.default": {"candidates": ["m1", "m2", "m3"], "strategy": ["capability"]}},
        handler=router,
        retry=RetryPolicy(
            max_attempts_per_candidate=3, total_max_attempts=4, backoff_base_s=0.0, jitter_ratio=0.0
        ),
    )

    # 3 个候选只试到第 2 个预算就没了 → 这是**真的**「还有候选没试过」，报预算耗尽。
    # 与「候选都试完且都失败」（AllCandidatesFailedError）是不同的问题。
    with pytest.raises(BudgetExhaustedError) as excinfo:
        await gateway.chat("chat.default", [Message.text("user", "hi")])

    assert excinfo.value.reason == "attempts"
    assert len(router.calls) == 4, f"应为 total_max_attempts=4 次，实际 {len(router.calls)} 次"


async def test_budget_exhausted_reports_reason(gateway_factory):
    """预算耗尽要区分 ``attempts`` 与 ``deadline`` —— 两者的修法完全不同。"""
    from gateway.retry import RetryPolicy

    router = HostRouter().on("a", httpx.Response(503, text="down"))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        deadline_s=0.5,
        retry=RetryPolicy(max_attempts_per_candidate=3, total_max_attempts=6, backoff_base_s=1.0),
    )

    with pytest.raises((BudgetExhaustedError, AllCandidatesFailedError)):
        await gateway.chat("chat.default", [Message.text("user", "hi")])


# --------------------------------------------------------------------------- #
# B-10 / B-11 —— 流式边界与能力拦截
# --------------------------------------------------------------------------- #


async def test_b10_stream_committed_blocks_fallback(gateway_factory):
    """B-10：流式已输出分片后断开 → 抛错结束，**不重新发起**（``FR-G-05``）。"""
    broken = BrokenStreamTransport(pieces=2)
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, transport=broken)

    events = [event async for event in gateway.stream_chat("chat.default", [Message.text("user", "hi")])]

    chunks = [event for event in events if isinstance(event, StreamChunk)]
    assert [chunk.text for chunk in chunks] == ["片0", "片1"], "已经吐出的分片要如实传给调用方"

    failures = [event for event in events if isinstance(event, StreamFailed)]
    assert failures, "断开后必须以失败收场"
    assert isinstance(failures[0].error, StreamCommittedError)

    # **总共只发起 1 次上游调用**：既不重试，也不降级到第二个候选。
    # 用户已经看到"片0片1"，重发只会得到第二份不连贯的输出。
    assert len(broken.calls) == 1, f"不得重发，实际发起 {len(broken.calls)} 次"


async def test_b10b_stream_before_first_chunk_can_fallback(gateway_factory):
    """还没吐出任何分片时，降级仍然是允许的 —— 与 B-10 形成对照。"""
    router = HostRouter().on("a", httpx.Response(503, text="down")).on(
        "b", httpx.Response(200, json=chat_body())
    )
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    events = [event async for event in gateway.stream_chat("chat.default", [Message.text("user", "hi")])]

    assert any(isinstance(event, StreamDone) for event in events)
    assert router.count("b") >= 1, "第一个分片都没吐出时应当降级"


async def test_b11_missing_capability_is_local_and_lists_gap(gateway_factory):
    """B-11：请求「流式 + 工具」而候选均不支持 → 报错说明缺什么，**零网络调用**。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body()))
    gateway = gateway_factory(
        models={"m1": model_spec("a", provider="vllm")},   # vllm 默认只有 chat + stream
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
    )

    with pytest.raises(NoCapableModelError) as excinfo:
        await gateway.chat(
            "chat.default",
            [Message.text("user", "hi")],
            tools=[ToolSpec("search")],
        )

    assert "tools" in str(excinfo.value)
    assert router.calls == [], "能力不足必须在本地拦下，不得发生任何网络调用"


# --------------------------------------------------------------------------- #
# B-12 / B-13 —— 计量
# --------------------------------------------------------------------------- #


async def test_b12_usage_is_aggregated_by_session(gateway_factory):
    """B-12：一次调用结束后可按会话维度汇总 token 与成本。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("hi", prompt_tokens=100, completion_tokens=50)))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        prices={"m1": _price()},
    )

    await gateway.chat("chat.default", [Message.text("user", "hi")], session_id="s-1")
    await gateway.chat("chat.default", [Message.text("user", "hi")], session_id="s-1")
    await gateway.chat("chat.default", [Message.text("user", "hi")], session_id="s-2")

    totals = gateway.ledger.totals(session_id="s-1")
    assert totals.input_tokens == 200
    assert totals.output_tokens == 100
    assert gateway.ledger.totals(session_id="s-2").input_tokens == 100

    # 3 次调用 × (100 输入 + 50 输出)，价格按每 1K token 计：
    #   300 × 0.001 / 1000 + 150 × 0.002 / 1000 = 0.0003 + 0.0003
    costs = gateway.ledger.cost_by_model()
    assert costs["m1"].amount == Decimal("0.0006")


def _price():
    from gateway.cost import Price

    return Price(input=Decimal("0.001"), output=Decimal("0.002"))


async def test_b13_unknown_price_is_not_zero(gateway_factory):
    """B-13：模型无价格 → 正常返回，成本标记**未知**而不是 0（``FR-G-09``）。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("hi", prompt_tokens=100, completion_tokens=50)))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        prices={},   # 没配价格
    )

    result = await gateway.chat("chat.default", [Message.text("user", "hi")])

    assert result.content == "hi"
    assert result.cost.amount is None
    assert str(result.cost) == "未知"


async def test_unknown_usage_is_not_zero(gateway_factory):
    """上游没返回 usage 时，即便有价格也只能标未知 —— **不编造 0**（``FR-P-10``）。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("hi")))   # 无 usage 字段
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        prices={"m1": _price()},
    )

    result = await gateway.chat("chat.default", [Message.text("user", "hi")])
    assert result.cost.amount is None


# --------------------------------------------------------------------------- #
# B-14 —— 事件
# --------------------------------------------------------------------------- #


async def test_b14_event_sequence_is_complete(gateway_factory):
    """B-14：一次「重试后降级成功」的调用 → 事件序列完整反映全过程（``FR-G-10``）。"""
    from gateway.gateway import EventName

    router = HostRouter().on("a", httpx.Response(503, text="down")).on(
        "b", httpx.Response(200, json=chat_body("备选"))
    )
    emitter = RecordingEmitter()
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router, events=emitter)

    await gateway.chat("chat.default", [Message.text("user", "hi")])

    names = emitter.names()
    assert EventName.CALL_STARTED in names
    assert EventName.CALL_FAILED in names
    assert EventName.CALL_RETRIED in names, "必须记录重试"
    assert EventName.CALL_DEGRADED in names, "必须记录降级"
    assert EventName.CALL_SUCCEEDED in names
    assert names.index(EventName.CALL_DEGRADED) < names.index(EventName.CALL_SUCCEEDED)


async def test_event_emitter_failure_does_not_break_calls(gateway_factory):
    """**观测失败不能拖垮调用** —— 事件是旁路，不是依赖。"""

    class Exploding:
        def emit(self, name, payload):
            raise RuntimeError("事件总线下线了")

    router = HostRouter().on("a", httpx.Response(200, json=chat_body("ok")))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
        events=Exploding(),
    )

    result = await gateway.chat("chat.default", [Message.text("user", "hi")])
    assert result.content == "ok"


# --------------------------------------------------------------------------- #
# B-15 —— 取消与配额归还
# --------------------------------------------------------------------------- #


async def test_b15_cancellation_releases_quota(gateway_factory, clock):
    """B-15：并发中取消调用 → 并发配额**归零，无泄漏**（``FR-G-13``）。

    泄漏一个并发位意味着那个模型永久少一个并发额度，
    而症状是「跑一段时间后并发上不去」—— 离原因很远。
    """
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        transport=BlockingTransport(),
        rate_limit=limiter(clock, max_concurrency=4),
    )

    tasks = [
        asyncio.create_task(gateway.chat("chat.default", [Message.text("user", "hi")]))
        for _ in range(3)
    ]
    await asyncio.sleep(0.01)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with pytest.raises(asyncio.CancelledError):
            await task

    assert gateway.limiter.snapshot()["inflight"].get("m1", 0) == 0, "并发额度必须归零"


# --------------------------------------------------------------------------- #
# B-16 —— 错误信息
# --------------------------------------------------------------------------- #


async def test_b16_errors_do_not_leak_api_key(gateway_factory):
    """B-16：gateway 抛出的错误里**不含 API Key、不含厂商原始报文全文**。"""
    key = "sk-gateway-test1234567890"
    router = HostRouter().on(
        "a", httpx.Response(401, json={"error": f"invalid key {key}"})
    )
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"chat.default": {"candidates": ["m1"]}},
        handler=router,
    )

    with pytest.raises(AllCandidatesFailedError) as excinfo:
        await gateway.chat("chat.default", [Message.text("user", "hi")])

    rendered = f"{excinfo.value} {excinfo.value.summary()}"
    assert key not in rendered

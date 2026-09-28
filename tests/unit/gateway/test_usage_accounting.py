"""用量记账的归属字段（``FR-G-08`` / ``FR-R-07`` 的接缝）。

**为什么单独一组**：这些字段的缺失**不会让任何功能出错** ——
调用照样成功、照样返回结果，只是落库之后按某个维度聚合会**静默少掉一批**。
本次架构分析在 gateway 里找到过三处同类的静默缺口，这个文件钉住其中的两处。
"""

from __future__ import annotations

import httpx

from gateway.types import GatewayResult
from gateway.usage import UsageLedger
from provider.types import Message

from .conftest import HostRouter, chat_body, model_spec

ALIAS = {"runtime.default": {"candidates": ["m1", "m2"], "strategy": ["capability"]}}
MODELS = {"m1": model_spec("a"), "m2": model_spec("b")}


async def test_failed_attempts_carry_their_alias(gateway_factory):
    """**失败记录必须带逻辑名。**

    这里曾经硬编码成空串，后果是 ``src/repo`` 侧按 ``alias`` 做的聚合会
    **静默丢掉全部失败记录** —— 而失败恰好是账单最容易对不上的地方，
    也正是最需要被单独看见的那一部分。

    注意断言的是「非空」而不是某个具体值：这条纪律的意义在于
    「这条记录还属于某个逻辑名」，而不在于它是哪一个。
    """
    router = HostRouter().on("a", httpx.Response(503, text="down"))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"runtime.default": {"candidates": ["m1"]}},
        handler=router,
    )

    try:
        await gateway.chat("runtime.default", [Message.text("user", "hi")])
    except Exception:  # noqa: BLE001 —— 这里只关心账本，不关心抛什么
        pass

    records = gateway.ledger.records
    assert records, "失败的调用也必须留下用量记录"
    assert all(r.alias == "runtime.default" for r in records), (
        f"失败记录的 alias 必须非空，实际：{[r.alias for r in records]}"
    )


async def test_degraded_attempt_is_marked_in_the_ledger(gateway_factory):
    """降级产生的记录要带 ``degraded`` 标记 —— 「这次特别贵」往往就是降级到了强模型。"""
    router = HostRouter().on("a", httpx.Response(503, text="down")).on(
        "b", httpx.Response(200, json=chat_body("备选"))
    )
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    await gateway.chat("runtime.default", [Message.text("user", "hi")])

    assert any(r.degraded for r in gateway.ledger.records), (
        "降级后的记录必须带 degraded 标记，否则「为什么这次贵了」查不出来"
    )


async def test_successful_call_records_usage_with_alias(gateway_factory):
    """成功路径也要带上逻辑名与会话 —— 三个聚合维度缺一不可。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("ok", prompt_tokens=7)))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"runtime.default": {"candidates": ["m1"]}},
        handler=router,
    )

    await gateway.chat(
        "runtime.default", [Message.text("user", "hi")], session_id="s-1", caller="agent-x"
    )

    record = gateway.ledger.records[-1]
    assert record.alias == "runtime.default"
    assert record.session_id == "s-1"
    assert record.caller == "agent-x"
    assert record.input_tokens == 7


# --------------------------------------------------------------------------- #
# drain_dropped —— 让「丢了多少」这件事有一个能被消费的出口
# --------------------------------------------------------------------------- #


async def test_drain_dropped_consumes_the_counter(gateway_factory):
    """**``drain_dropped`` 必须清零。**

    交付方是**周期性**调用的。如果它只读不清，同一笔丢弃会被反复上报 ——
    「丢了 3 条」会被记成 3、6、9…… 而每一次看起来都像是新丢的。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("ok")))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"runtime.default": {"candidates": ["m1"]}},
        handler=router,
        ledger=UsageLedger(max_records=2),
    )

    for _ in range(5):
        await gateway.chat("runtime.default", [Message.text("user", "hi")])

    first = gateway.ledger.drain_dropped()
    assert first > 0, "写 5 条、上限 2 条，必然有丢弃"
    assert gateway.ledger.drain_dropped() == 0, "取走之后必须清零，否则同一笔会被重复上报"


async def test_drain_and_drain_dropped_are_separate(gateway_factory):
    """记录与丢弃数是**两个**出口 —— 合在一起会让「记录为空但仍丢过数据」无法表达。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("ok")))
    gateway = gateway_factory(
        models={"m1": model_spec("a")},
        aliases={"runtime.default": {"candidates": ["m1"]}},
        handler=router,
        ledger=UsageLedger(max_records=1),
    )

    for _ in range(3):
        await gateway.chat("runtime.default", [Message.text("user", "hi")])

    records = gateway.ledger.drain()
    dropped = gateway.ledger.drain_dropped()
    assert len(records) == 1
    assert dropped == 2

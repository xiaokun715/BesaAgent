"""组合根的交付：``Runtime.flush_pending()`` 把用量与事件一起落库。

**为什么必须单独测这条**：它是**唯一**把三层接起来的地方 ——
gateway 产出、sink 映射、repo 落库，任何一环的签名不匹配都只在这里暴露。

这条用例是补出来的：实现 ``flush_pending`` 时把
``record_ledger(*self._take_usage())`` 写成了位置参数展开，而
``dropped`` 是关键字参数 —— 207 个测试全绿，直到真跑一次端到端才炸。
**单元测试覆盖了每一环，却没有覆盖「它们接起来」这一环。**

用内存 SQLite 跑（``NFR-R-04``），不依赖本机数据库。
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from composition.bootstrap import build_runtime
from foundation.database import Database
from foundation.db import Base, create_engine
from foundation.logging import reset_trace_id, set_trace_id
from foundation.settings import Settings
from provider.types import Message

# 让两张表都注册到 Base.metadata（create_all 只建此刻已注册的表）
import repo.event  # noqa: F401
import repo.usage  # noqa: F401

# 复用 gateway 单测的假端点 —— 「按 host 分发 + 记录调用次数」那套。
from tests.unit.gateway.conftest import HostRouter, chat_body, model_spec


@pytest.fixture
async def db():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.aclose()


def _settings() -> Settings:
    return Settings(
        env="probe",
        data={
            "providers": {"openai": {"base_url": "https://a/v1", "api_key_env": "OPENAI_API_KEY"}},
            "models": {"m1": model_spec("a"), "m2": model_spec("b")},
            "gateway": {
                "aliases": {
                    "runtime.default": {"candidates": ["m1", "m2"], "strategy": ["capability"]}
                },
                "retry": {
                    "max_attempts_per_candidate": 1,
                    "total_max_attempts": 3,
                    "backoff_base_s": 0,
                    "jitter_ratio": 0,
                },
            },
        },
        sources=("<inline>",),
        env_vars={"OPENAI_API_KEY": "sk-test"},
    )


@pytest.fixture
def degraded_router() -> HostRouter:
    """主候选挂、备选正常 —— 会走「失败 → 降级 → 成功」，事件序列最完整。"""
    return HostRouter().on("a", httpx.Response(503, text="down")).on(
        "b", httpx.Response(200, json=chat_body("备选"))
    )


async def test_close_flushes_usage_and_events(db: Database, degraded_router: HostRouter):
    """**关停时把待落库的用量与事件一起写下去，且同事务。**

    这条测的是 ``NFR-R-07`` 与 ``gateway`` 那个已知缺口的结合部：
    ``Gateway.aclose()`` 不 drain 它的账本/缓冲 —— 不补这一步，
    数据会跟着进程一起消失，而且**不会有任何错误**。
    """
    token = set_trace_id("it-dlv-1")
    try:
        runtime = build_runtime(
            _settings(),
            database=db,
            provider_options={"transport": httpx.MockTransport(degraded_router)},
        )
        result = await runtime.gateway.chat(
            "runtime.default", [Message.text("user", "hi")], session_id="it-sess"
        )
        assert result.degraded is True

        # 关停前：数据都还在内存里，库里什么都没有
        async with db.transaction() as tx:
            assert await tx.scalar(text("SELECT count(*) FROM usage")) == 0, (
                "关停前不该已经落库 —— 否则这条用例测不到 flush 本身"
            )

        await runtime.aclose()  # ← aclose 必须先 flush 再关连接
    finally:
        reset_trace_id(token)

    async with db.transaction() as tx:
        usage_count = await tx.scalar(text("SELECT count(*) FROM usage"))
        events = (
            await tx.execute(text("SELECT name FROM event ORDER BY id"))
        ).scalars().all()

    assert usage_count > 0, "关停时必须把用量刷下去"
    assert events, "关停时必须把事件刷下去"

    # 一次「失败 → 降级 → 成功」的调用，事件序列要能还原全过程（B-14）
    assert events[0] == "gateway.call.started"
    assert events[-1] == "gateway.call.succeeded"
    assert "gateway.call.failed" in events
    assert "gateway.call.degraded" in events


async def test_usage_and_events_share_the_trace_id(db: Database, degraded_router: HostRouter):
    """**用量与事件的 trace_id 必须能对上。**

    它们来自**两个不同的来源**：用量的来自 ``chat(trace_id=...)`` 的显式参数，
    事件的来自 ``foundation.logging`` 的 contextvar。
    调用方不传显式参数时，前者是空串而后者有值 ——
    于是两张表虽然同属一次调用，却**关联不起来**，而它们恰恰是靠 trace_id 串成一条线的。

    ``usage_sink.to_usage_rows`` 在源头做了归一化（显式优先，缺了取上下文）。
    """
    token = set_trace_id("it-dlv-2")
    try:
        runtime = build_runtime(
            _settings(),
            database=db,
            provider_options={"transport": httpx.MockTransport(degraded_router)},
        )
        # **刻意不传 trace_id** —— 这正是上面说的那种调用方式
        await runtime.gateway.chat("runtime.default", [Message.text("user", "hi")])
        await runtime.aclose()
    finally:
        reset_trace_id(token)

    async with db.transaction() as tx:
        usage_traces = (
            await tx.execute(text("SELECT DISTINCT trace_id FROM usage"))
        ).scalars().all()
        event_traces = (
            await tx.execute(text("SELECT DISTINCT trace_id FROM event"))
        ).scalars().all()

    assert usage_traces == ["it-dlv-2"], "用量必须也带上上下文的 trace_id"
    assert event_traces == ["it-dlv-2"]
    assert set(usage_traces) == set(event_traces), "两张表必须能按 trace_id 关联"


async def test_flush_is_safe_to_call_twice(db: Database, degraded_router: HostRouter):
    """重复 flush 不重复写 —— 关停路径可能被调用多次（``aclose`` 要求幂等）。"""
    runtime = build_runtime(
        _settings(),
        database=db,
        provider_options={"transport": httpx.MockTransport(degraded_router)},
    )
    await runtime.gateway.chat("runtime.default", [Message.text("user", "hi")])

    await runtime.flush_pending()
    async with db.transaction() as tx:
        first = await tx.scalar(text("SELECT count(*) FROM usage"))

    await runtime.flush_pending()
    async with db.transaction() as tx:
        second = await tx.scalar(text("SELECT count(*) FROM usage"))

    assert first == second, "第二次 flush 没有东西可交，不该重复写"
    await runtime.aclose()


async def test_without_database_nothing_is_persisted(db: Database, degraded_router: HostRouter):
    """**不注入 database 时（CLI 默认）不落库**，且行为与以前一致。

    这是有意的：内存 SQLite 没有表（CLI 不走迁移），贸然注入会让每次 flush
    都以「表不存在」失败并刷告警 —— 一个每次都报错但功能正常的告警比没有告警更糟。
    """
    runtime = build_runtime(
        _settings(),
        provider_options={"transport": httpx.MockTransport(degraded_router)},
    )
    assert runtime.database is None
    assert runtime.event_sink is None, "不落库时不该攒事件（攒着也没地方去）"

    await runtime.gateway.chat("runtime.default", [Message.text("user", "hi")])
    await runtime.aclose()  # 不该抛异常

    async with db.transaction() as tx:
        assert await tx.scalar(text("SELECT count(*) FROM usage")) == 0

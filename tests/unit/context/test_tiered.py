"""三层策略：短期 / 会话 / 长期（`FR-C-07..11`）。

**为什么这组值得写**：过渡区是**N 与 K 不同步**的必然产物，而它最容易被漏掉 ——
只写「窗口 + 摘要」时，那段历史是**静默消失**的，没有任何东西会报错。
所以这里最重的断言是：「**出了窗口、还没压缩的那几轮，还在不在**」。
"""

from __future__ import annotations

from context.compact.boundaries import flatten
from context.context import ContextRequest
from context.strategies import InMemorySessionStore, build_strategies
from tests.unit.context.conftest import FakeMemory, history, history as _h, make_manager


def _request(turns: int, **overrides) -> ContextRequest:
    base = dict(session_id="s-tiered", history=history(turns, size=60), current_input="继续")
    base.update(overrides)
    return ContextRequest(**base)


def _tiered(compactor, *, store=None, memory=None, **tiered_cfg):
    from context.resolver import MemoryResolver

    strategies = build_strategies(
        compactor=compactor,
        resolver=MemoryResolver(memory),
        tiered_cfg=tiered_cfg,
        session_store=store,
    )
    return strategies["tiered"]


def _window_turns(collected) -> int:
    """window 段里有多少轮（按 user 消息数）。"""
    return sum(1 for b in collected.blocks.get("window", ()) if b.role == "user")


# ---------------------------------------------------------------- 短期
async def test_window_keeps_the_last_n_turns_raw(chat, compactor):
    strategy = _tiered(compactor, window_size=10, compress_every=100, transition_max_rounds=100)
    collected = await strategy.collect(_request(25), target_window_tokens=100_000)

    assert _window_turns(collected) == 10
    assert all(b.provenance.level == "L0" for b in collected.blocks["window"]), "窗口内是**原文**"


async def test_window_never_gets_compressed(chat, compactor):
    """窗口是"工作记忆"，压它等于让模型忘记自己正在做什么。"""
    strategy = _tiered(compactor, window_size=5, compress_every=100, transition_max_rounds=100)
    collected = await strategy.collect(_request(12), target_window_tokens=100_000)
    assert all(b.provenance.level == "L0" for b in collected.blocks["window"])


# ---------------------------------------------------------------- 过渡区
async def test_transition_is_kept_raw_when_compression_is_not_due(chat, compactor):
    """**这是本策略最容易漏掉的一层**：出了窗口、还没到 K 轮 —— 必须先原样保留。"""
    strategy = _tiered(compactor, window_size=5, compress_every=100, transition_max_rounds=100)
    collected = await strategy.collect(_request(8), target_window_tokens=100_000)

    transition = collected.blocks.get("transition", ())
    assert transition, "过渡区不能是空的 —— 那说明这段历史被静默丢掉了"
    # 8 轮、窗口 5 → 前 3 轮在过渡区
    assert len([b for b in transition if b.role == "user"]) == 3
    assert all(b.provenance.level == "L0" for b in transition), "还没压 → 原样"


async def test_transition_is_compressed_on_the_kth_turn(chat, compactor):
    """到 K 轮就把过渡区并进摘要链。装得下时走 **L1（零成本）**，不该调模型。"""
    strategy = _tiered(compactor, window_size=5, compress_every=3, transition_max_rounds=100)
    collected = await strategy.collect(_request(8), target_window_tokens=100_000)

    assert not collected.blocks.get("transition"), "到 K 轮后过渡区应当并入摘要链"
    summaries = collected.blocks.get("session_summary", ())
    assert summaries and summaries[0].provenance.compacted
    assert chat.call_count == 0, "装得下就走零成本的 L1 —— 级联的第一条约束"


async def test_transition_uses_llm_when_it_does_not_fit(chat, compactor):
    """装不下才升 L2 —— 这时才该**花一次模型调用**。"""
    strategy = _tiered(compactor, window_size=2, compress_every=2, transition_max_rounds=100)
    collected = await strategy.collect(_request(10), target_window_tokens=40)

    assert collected.blocks.get("session_summary")
    assert chat.call_count >= 1


async def test_transition_over_limit_is_force_compressed(chat, compactor):
    """K > N 时过渡区会一直涨 —— **上限**是唯一的刹车（`FR-C-09`）。"""
    strategy = _tiered(compactor, window_size=3, compress_every=1000, transition_max_rounds=2)
    collected = await strategy.collect(_request(10), target_window_tokens=100_000)

    assert not collected.blocks.get("transition"), "超过上限就该**强制压缩**，不等第 K 轮"
    assert collected.blocks.get("session_summary")
    assert any("过渡区已压缩" in note for note in collected.notes)


# ---------------------------------------------------------------- 游标
async def test_summarized_turns_are_not_compressed_twice(chat, compactor):
    """游标是"已经压到第几轮" —— 没有它就会重复压缩同一段。"""
    store = InMemorySessionStore()
    strategy = _tiered(compactor, store=store, window_size=3, compress_every=3, transition_max_rounds=100)

    await strategy.collect(_request(7), target_window_tokens=100_000)
    calls_after_first = chat.call_count
    await strategy.collect(_request(7), target_window_tokens=100_000)

    assert chat.call_count == calls_after_first, "同一批轮次不该被压第二次"
    assert store.get("s-tiered").summarized_turns == 4   # 7 轮 - 窗口 3


async def test_cursor_is_clamped_when_history_shrinks(chat, compactor):
    """会话历史变短（编辑/回滚）时游标不能越界 —— 否则过渡区会算成负数。"""
    store = InMemorySessionStore()
    strategy = _tiered(compactor, store=store, window_size=3, compress_every=3, transition_max_rounds=100)
    await strategy.collect(_request(10), target_window_tokens=100_000)
    collected = await strategy.collect(_request(4), target_window_tokens=100_000)   # 变短了
    assert _window_turns(collected) == 3


# ---------------------------------------------------------------- 摘要链
async def test_summary_chain_is_digested_when_over_quota(chat, compactor):
    """摘要链自己也会变长 —— 那时用的就是**多级压缩的 L3**（两条策略在这里汇合）。"""
    from context.compact.levels import Compactor
    from context.compressor import build_compactor

    deep = build_compactor(chat=chat, cfg={"max_level": "L3"}, clock=None)
    strategy = _tiered(deep, window_size=1, compress_every=1, transition_max_rounds=100)

    for turns in (2, 3, 4, 5):
        await strategy.collect(_request(turns), target_window_tokens=40)   # 配额极小 → 逼出 L3

    assert chat.call_count >= 1


# ---------------------------------------------------------------- 长期
async def test_long_term_memory_is_injected(chat, compactor):
    memory = FakeMemory(memories=[{"content": "本仓库用 pytest 做测试，这是长期记忆条目" * 3, "score": 0.9}])
    strategy = _tiered(compactor, memory=memory, window_size=5, compress_every=100, transition_max_rounds=100)
    collected = await strategy.collect(_request(6), target_window_tokens=100_000)

    long_term = collected.blocks.get("long_term", ())
    assert len(long_term) == 1
    assert "不可信记忆" in long_term[0].content


async def test_long_term_can_be_turned_off(chat, compactor):
    memory = FakeMemory(memories=[{"content": "x" * 200}])
    strategy = _tiered(
        compactor, memory=memory, window_size=5, compress_every=100,
        transition_max_rounds=100, long_term={"enabled": False},
    )
    collected = await strategy.collect(_request(6), target_window_tokens=100_000)
    assert "long_term" not in collected.blocks


def test_k_greater_than_n_still_works():
    """`C-C-5`：K > N 是合法配置（会走过渡区），只是要让人知道。"""
    from context.strategies import TieredPolicy

    policy = TieredPolicy.from_config({"window_size": 3, "compress_every": 10})
    assert policy.window_size == 3 and policy.compress_every == 10


def test_invalid_tiered_config_is_rejected():
    import pytest

    from context.strategies import TieredPolicy

    with pytest.raises(ValueError):
        TieredPolicy.from_config({"window_size": 0})
    with pytest.raises(ValueError):
        TieredPolicy.from_config({"compress_every": 0})


# ---------------------------------------------------------------- 与 manager 串起来
async def test_manager_can_run_the_tiered_strategy(chat, clock):
    manager = make_manager(
        chat=chat, clock=clock, strategy="tiered",
        context_cfg={"tiered": {"window_size": 5, "compress_every": 100, "transition_max_rounds": 100}},
    )
    built = await manager.build(_request(8))
    assert built.ok
    assert built.strategy == "tiered"
    segments = {b.provenance.segment for b in built.blocks}
    assert "window" in segments

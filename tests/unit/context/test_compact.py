"""多级压缩的级联（`FR-C-03..06`）。

**为什么这组值得写**：级联的全部价值在「**够用的最低成本**」——
该只做 L1 的时候调了模型就是白花钱，该升 L2 的时候不升就是超预算，
而这两种错**都不会报错**。所以核心断言是「**调了几次模型**」与「**压到第几级**」。
"""

from __future__ import annotations

from context.compact.cache import CompactCache
from context.compact.levels import Compactor, denoise
from context.compressor import build_compactor
from context.types import ContextBlock, ContextMessage, Provenance, ToolCallRef
from tests.unit.context.conftest import FakeChat, history, turn


def _blocks(level: str | None = None, blocks=()):
    return [b for b in blocks if level is None or b.provenance.level == level]


# ---------------------------------------------------------------- L0 / 不压
async def test_short_history_is_not_compressed_at_all(chat, clock):
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 10}, clock=clock)
    result = await compactor.compact(history(3, size=5), target_tokens=10)
    assert result.level_used == "L0"
    assert chat.call_count == 0, "轮数不足保护轮数时**一次模型都不该调**"


# ---------------------------------------------------------------- L1
async def test_l1_only_when_it_is_enough(chat, clock):
    """L1 是唯一零成本的一级 —— 够用时**绝不调模型**。"""
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 2}, clock=clock)
    result = await compactor.compact(history(6, size=5), target_tokens=100_000)
    assert result.level_used == "L1"
    assert chat.call_count == 0
    assert any(e.level == "L1" for e in result.entries)


async def test_l1_truncates_tool_output_with_a_marker(chat, clock):
    compactor = build_compactor(
        chat=chat, cfg={"keep_recent_turns": 0, "l1_max_tool_output_chars": 20}, clock=clock
    )
    result = await compactor.compact(history(2, tool=True, size=100), target_tokens=100_000)
    tool_blocks = [b for b in result.blocks if b.role == "tool"]
    assert tool_blocks
    assert "L1 截断" in tool_blocks[0].content, "截断**必须留下标记**，否则读的人以为那就是全部"


async def test_l1_never_breaks_tool_pairing(chat, clock):
    """L1 **不删消息** —— 删一条 tool 结果会把配对切断。"""
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 0}, clock=clock)
    original = history(3, tool=True, size=200)
    result = await compactor.compact(original, target_tokens=1_000_000)
    assert len(_blocks(blocks=result.blocks)) == len(original)


def test_denoise_is_near_lossless():
    messages = (
        ContextMessage(role="user", content="第一行\n\n\n\n第三行"),
        ContextMessage(role="assistant", content="  首尾空白  "),
    )
    cleaned = denoise(messages, max_tool_chars=100)
    assert cleaned[0].content == "第一行\n\n第三行"
    assert cleaned[1].content == "首尾空白"


# ---------------------------------------------------------------- L2
async def test_l2_is_used_when_l1_is_not_enough(chat, clock):
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 1}, clock=clock)
    result = await compactor.compact(history(10, size=200), target_tokens=200)
    assert result.level_used in ("L2", "L3")
    assert chat.call_count == 1, "L2 只该调**一次**模型"
    summary = [b for b in result.blocks if b.provenance.compacted]
    assert summary and summary[0].provenance.message_ids, "摘要必须带**覆盖范围**（回捞钩子）"


async def test_summary_carries_a_structured_marker(chat, clock):
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 1}, clock=clock)
    result = await compactor.compact(history(10, size=200), target_tokens=200)
    block = next(b for b in result.blocks if b.provenance.compacted)
    text = block.to_model_text()
    assert "<<<compacted level=" in text and "range=" in text
    assert "可能丢失细节" in text, "模型要知道自己在看摘要，而不是原文"


async def test_tail_is_protected(chat, clock):
    """**只压中段**：尾部 ``keep_recent_turns`` 轮原样保留。"""
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 3}, clock=clock)
    original = history(10, size=200)
    result = await compactor.compact(original, target_tokens=200)

    tail = [b for b in result.blocks if b.provenance.level == "L0"]
    assert len(tail) == 6, "最近 3 轮（6 条消息）应当原样保留"
    assert [b.message for b in tail] == list(original[-6:])


async def test_llm_calls_are_bounded(chat, clock):
    """压缩不能成为成本的第二个来源（`NFR-C-09`）。"""
    compactor = build_compactor(
        chat=chat, cfg={"keep_recent_turns": 1, "max_level": "L3", "max_llm_calls": 1}, clock=clock
    )
    await compactor.compact(history(12, size=300), target_tokens=1)
    assert chat.call_count <= 1


# ---------------------------------------------------------------- 缓存
async def test_compaction_is_cached(chat, clock):
    """没有缓存就**每轮重压一次** —— 成本随轮数线性涨，而没有一行代码看起来是错的。"""
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 1, "cache": True}, clock=clock)
    messages = history(10, size=200)

    first = await compactor.compact(messages, target_tokens=200)
    calls_after_first = chat.call_count
    second = await compactor.compact(messages, target_tokens=200)

    assert chat.call_count == calls_after_first, "第二次同内容应当命中缓存"
    assert second.cached_hits == 1
    assert [b.content for b in first.blocks] == [b.content for b in second.blocks]


def test_cache_key_includes_prompt_version():
    """提示词改了不能用旧摘要 —— 那会拿到"用旧标准压的"结果，没有任何症状。"""
    a = CompactCache.key("fp", "L2", "v1")
    b = CompactCache.key("fp", "L2", "v2")
    assert a != b
    assert CompactCache.key("fp", "L2", "v1") == a


# ---------------------------------------------------------------- 失败与到顶
async def test_llm_failure_degrades_instead_of_raising(chat, clock):
    """压缩是**优化**，不该变成新的故障点（`NFR-C-07`）。"""
    chat.fail = True
    compactor = build_compactor(chat=chat, cfg={"keep_recent_turns": 1}, clock=clock)
    result = await compactor.compact(history(10, size=200), target_tokens=50)
    assert result.level_used == "L1"
    assert result.insufficient, "压不动就要**如实上报**"
    assert len(result.blocks) == len(history(10, size=200)) or result.blocks


async def test_max_level_caps_the_cascade(chat, clock):
    """到 ``max_level`` 仍不够 → 如实报告，**不擅自再升一级**。"""
    compactor = build_compactor(
        chat=chat, cfg={"keep_recent_turns": 1, "max_level": "L1"}, clock=clock
    )
    result = await compactor.compact(history(10, size=400), target_tokens=10)
    assert result.level_used == "L1"
    assert result.insufficient
    assert chat.call_count == 0, "max_level=L1 时**不该调模型**"


async def test_no_summarizer_falls_back_to_l1_only(clock):
    """摘要器不可用时**照样工作** —— L1 是零成本的，能压掉一部分。"""
    compactor = build_compactor(chat=None, cfg={"keep_recent_turns": 1}, clock=clock)
    result = await compactor.compact(history(8, size=200), target_tokens=100)
    assert result.level_used == "L1"
    assert result.insufficient and "摘要器不可用" in result.note


async def test_digest_compresses_a_summary_chain(chat, clock):
    """摘要链自身超配额 → L3。**这是两条策略共用零件的地方。**"""
    compactor = build_compactor(
        chat=chat, cfg={"keep_recent_turns": 1, "max_level": "L3"}, clock=clock
    )
    chain = tuple(
        ContextBlock(
            content="旧摘要" * 200,
            role="user",
            provenance=Provenance(segment="session_summary", level="L2", message_ids=(f"m{i}",)),
        )
        for i in range(3)
    )
    result = await compactor.digest(chain, target_tokens=100)
    assert result.level_used == "L3"
    assert len(result.blocks) == 1
    assert result.blocks[0].provenance.message_ids, "**范围要并上去** —— 否则回捞的钩子在这级断掉"


async def test_digest_is_a_noop_when_within_quota(chat, clock):
    compactor = build_compactor(chat=chat, clock=clock)
    chain = (ContextBlock(content="短摘要", provenance=Provenance(segment="session_summary", level="L2")),)
    result = await compactor.digest(chain, target_tokens=1_000)
    assert result.level_used == "L2"
    assert chat.call_count == 0

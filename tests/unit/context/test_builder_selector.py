"""组装（`FR-C-14`）与去重/裁剪（`FR-C-11` / `FR-C-13`）。

**为什么这组值得写**：组装出来的东西是**直接发给厂商的**。
配对错了厂商 400，而报文只说「第 N 项参数非法」—— 上下文是动态拼的，
出问题时调用方自己也不知道拼成了什么。所以这里每条断言都冲着"发出去合法吗"。
"""

from __future__ import annotations

import pytest
from provider.types import message_text

from context.builder import build_messages
from context.selector import dedupe, fit
from context.types import (
    BudgetPlan,
    ContextBlock,
    ContextMessage,
    Provenance,
    ToolCallRef,
)


def _text(message) -> str:
    return message_text(message) if not isinstance(message.content, str) else message.content


def _block(content: str, *, segment: str = "window", level: str = "L0", role: str = "assistant", message=None):
    return ContextBlock(
        content=content,
        role=role,  # type: ignore[arg-type]
        provenance=Provenance(segment=segment, level=level),  # type: ignore[arg-type]
        message=message,
    )


# ---------------------------------------------------------------- 组装
def test_system_is_first_and_current_input_is_last():
    messages = build_messages(
        system="你是助手",
        blocks=[_block("历史一"), _block("历史二")],
        current_input="当前问题",
    )
    assert messages[0].role == "system"
    assert messages[-1].role == "user"
    assert _text(messages[-1]) == "当前问题"


def test_tool_call_and_result_are_rebuilt_with_ids():
    """工具调用与结果必须带着 id 才能配对 —— 丢了 id 厂商直接 400。"""
    call = ToolCallRef(id="c1", name="read", arguments={"path": "a"}, arguments_raw='{"path":"a"}')
    history = (
        ContextMessage(role="user", content="读一下", message_id="u1"),
        ContextMessage(role="assistant", content="", message_id="a1", tool_calls=(call,)),
        ContextMessage(role="tool", content="文件内容", message_id="t1", tool_call_id="c1"),
    )
    blocks = tuple(
        ContextBlock(content=m.content, role=m.role, provenance=Provenance(segment="window"), message=m)
        for m in history
    )
    messages = build_messages(system="s", blocks=blocks, current_input="继续")

    # [system, user, assistant(工具调用), tool(结果), user(当前输入)]
    assert messages[2].tool_calls and messages[2].tool_calls[0].id == "c1"
    assert messages[2].tool_calls[0].arguments_raw == '{"path":"a"}'
    assert messages[3].role == "tool" and messages[3].tool_call_id == "c1"


def test_historical_system_messages_merge_into_the_leading_one():
    """一个请求里**只该有一条 system**。历史里若出现，就并进去，而不是再发一条。"""
    history = ContextMessage(role="system", content="历史 system 提示")
    blocks = (
        ContextBlock(content=history.content, role="system",
                     provenance=Provenance(segment="window"), message=history),
    )
    messages = build_messages(system="主提示", blocks=blocks, current_input="问")
    assert sum(1 for m in messages if m.role == "system") == 1
    assert "历史 system 提示" in _text(messages[0])


def test_compacted_block_becomes_a_user_message_with_marker():
    """摘要用 ``user`` 角色 + 结构化标记 —— 避开连续同角色，也不让模型当成自己说过的话。"""
    block = _block("（摘要）沿用 A 方案", level="L2", segment="window", role="user")
    messages = build_messages(system="s", blocks=[block], current_input="问")
    body = messages[1]
    assert body.role == "user"
    assert "<<<compacted level=L2" in _text(body)


def test_empty_assistant_block_is_skipped():
    block = _block("", role="assistant", message=ContextMessage(role="assistant", content=""))
    messages = build_messages(system="s", blocks=[block], current_input="问")
    assert len(messages) == 2   # system + current


def test_empty_system_falls_back_to_a_default():
    """空 system 在某些厂商上等于「没有指令」。"""
    messages = build_messages(system="   ", blocks=(), current_input="问")
    assert _text(messages[0]).strip()


def test_empty_current_input_is_a_programming_error():
    with pytest.raises(ValueError, match="current_input 为空"):
        build_messages(system="s", blocks=(), current_input="   ")


# ---------------------------------------------------------------- 去重
def test_dedupe_removes_cross_segment_duplicates():
    long_text = "这是一条足够长的、会跨段重复出现的内容" * 3
    blocks = {
        "window": (_block(long_text),),
        "long_term": (_block(long_text, segment="long_term", role="user"),),
    }
    kept, dropped = dedupe(blocks)
    assert len(kept["window"]) == 1 and len(kept["long_term"]) == 0
    assert dropped and "long_term" in dropped[0]


def test_dedupe_keeps_the_higher_fidelity_one():
    """原文 > 结构化条目 > 压缩摘要 —— 判据是**保真度**，因为摘要是有损的。"""
    long_text = "同样的内容出现在摘要与原文里" * 5
    blocks = {
        "window": (_block(long_text),),
        "session_summary": (_block(long_text, segment="session_summary", level="L2", role="user"),),
    }
    kept, _ = dedupe(blocks)
    assert len(kept["window"]) == 1
    assert kept["session_summary"] == ()


def test_dedupe_is_conservative_for_short_blocks():
    """**错去重是丢信息** —— 短句（「好的」「继续」）撞车太容易，不参与去重。"""
    blocks = {"window": (_block("继续"),), "long_term": (_block("继续", segment="long_term"),)}
    kept, dropped = dedupe(blocks)
    assert len(kept["window"]) == 1 and len(kept["long_term"]) == 1
    assert dropped == ()


def test_dedupe_can_be_disabled():
    long_text = "足够长的重复内容" * 5
    blocks = {"window": (_block(long_text),), "long_term": (_block(long_text, segment="long_term"),)}
    kept, dropped = dedupe(blocks, enabled=False)
    assert len(kept["long_term"]) == 1 and dropped == ()


# ---------------------------------------------------------------- 裁剪
def test_fit_keeps_whole_blocks_and_reports_drops():
    """**整块保留或整块丢弃** —— 半截摘要比没有摘要更误导。"""
    blocks = {
        "window": (
            ContextBlock(content="x" * 400, provenance=Provenance(segment="window")),
            ContextBlock(content="y" * 400, provenance=Provenance(segment="window")),
        )
    }
    plan = BudgetPlan(model_window=1000, available_input=1000, used={"window": 150})
    kept, notes = fit(blocks, plan)
    assert len(kept["window"]) == 1
    assert notes and "超出配额" in notes[0]

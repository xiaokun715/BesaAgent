"""切分点与配对（`FR-C-04` / `FR-C-14`）。

**为什么这组值得写**：按条数切分是最自然的写法，也是**必然出错**的写法 ——
它会把工具调用与结果分到两侧，而厂商的报错只说「消息顺序不对」，
指不到真正的问题。所以"按轮切"这条纪律必须有测试盯着。
"""

from __future__ import annotations

from context.compact.boundaries import by_turn, flatten, split_tail, validate_pairing
from context.types import ContextMessage, ToolCallRef
from tests.unit.context.conftest import history, turn


def test_turn_starts_at_user_message():
    turns = by_turn(history(3))
    assert len(turns) == 3
    assert all(t[0].role == "user" for t in turns)
    assert [len(t) for t in turns] == [2, 2, 2]


def test_tool_call_and_result_stay_in_the_same_turn():
    """**这是「按轮切」的全部意义**：调用与结果天然不会被切开。"""
    turns = by_turn(history(3, tool=True))
    assert len(turns) == 3
    for t in turns:
        roles = [m.role for m in t]
        assert roles == ["user", "assistant", "tool"]


def test_leading_non_user_messages_form_their_own_turn():
    """开头的非 user 消息**自成一轮**，而不是被丢掉 —— 丢掉也是静默丢内容。"""
    messages = (ContextMessage(role="assistant", content="我先说一句"),) + history(1)
    assert len(by_turn(messages)) == 2


def test_split_tail_protects_recent_turns():
    turns = by_turn(history(10))
    head, tail = split_tail(turns, keep_recent=3)
    assert len(head) == 7 and len(tail) == 3


def test_split_tail_with_zero_keeps_everything_compressible():
    turns = by_turn(history(4))
    head, tail = split_tail(turns, keep_recent=0)
    assert len(head) == 4 and tail == ()


def test_flatten_round_trips():
    original = history(3)
    assert flatten(by_turn(original)) == original


def test_pairing_is_valid_for_a_healthy_history():
    assert validate_pairing(history(3, tool=True)) == ()


def test_pairing_detects_a_dangling_call():
    """调用留下了、结果被切走了 —— **必须被本地拦下**。"""
    messages = [
        ContextMessage(role="user", content="读一下"),
        ContextMessage(
            role="assistant", content="",
            tool_calls=(ToolCallRef(id="c1", name="read"),),
        ),
    ]
    problems = validate_pairing(tuple(messages))
    assert problems and "配对被切断了" in problems[0]


def test_pairing_detects_an_orphan_result():
    messages = [
        ContextMessage(role="user", content="读一下"),
        ContextMessage(role="tool", content="内容", tool_call_id="c1"),
    ]
    problems = validate_pairing(tuple(messages))
    assert problems and "不在结果里" in problems[0]


def test_pairing_detects_a_tool_result_without_id():
    messages = [
        ContextMessage(role="user", content="读一下"),
        ContextMessage(role="tool", content="内容"),
    ]
    problems = validate_pairing(tuple(messages))
    assert any("没有 tool_call_id" in p for p in problems)

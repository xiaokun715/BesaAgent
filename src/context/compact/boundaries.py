"""切分点：**按「轮」，不许切断配对**（`FR-C-04` / `DC-5`）。

**这是整个压缩机制里最容易做错、也最值得单独成文件的一处。**

按消息**条数**切是最自然的写法，也是**必然出错**的写法：一条工具调用（assistant）
与它的结果（tool）会被分到切分点两侧。厂商的报错说的是"消息顺序不对"，
既不说是哪条消息，也不说少了什么 —— 而上下文层正是"动态拼出来的"，出问题时
调用方自己也不知道拼成了什么。

所以**切分的唯一合法单位是「轮」**：

    一轮 = 一条 user 消息起，到下一个 user 消息之前
           （含中间所有 assistant / tool 消息）

这样定义，"调用与结果"**天然落在同一轮里**，不需要额外的配对修补逻辑。

**「什么算一轮」只有这一份定义**（`MC-9`）：窗口边界（策略二）与压缩边界（策略一）
都调这里。两处各写一份的后果是它们会漂移，表现为"同一句话被带上两次"。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from context.types import ContextMessage

__all__ = [
    "by_step",
    "by_turn",
    "fingerprint",
    "flatten",
    "split_tail",
    "validate_pairing",
]


def by_step(messages: Sequence[ContextMessage]) -> tuple[tuple[ContextMessage, ...], ...]:
    """按「步」切分 —— **一次运行内部的切分单位**。

        一步 = 一次 assistant 消息 + 紧随其后的**全部**工具结果

    为什么需要它（`docs/agent` `MA-2`）：一次 agent 运行内部的消息是

        [user(任务), assistant(工具调用), tool, assistant(工具调用), tool, …]

    它们**全部属于同一「轮」**（只有一条 user 消息），所以 :func:`by_turn`
    会把它整个当成一轮 —— **一步都压不掉**。而工具结果的体量是实打实的
    （单条大结果落盘上限 64KB），二十步足够撑爆任何窗口。

    **开头的非 assistant 消息自成一组**（通常就是任务本身）。
    调用方应当把它**钉住不参与压缩** —— 任务陈述被压掉等于 agent 忘了自己要干什么。
    """
    groups: list[tuple[ContextMessage, ...]] = []
    current: list[ContextMessage] = []
    for message in messages:
        # 每条 assistant 开始新的一步；开头的非 assistant（任务）自成一组。
        if message.role == "assistant" and current:
            groups.append(tuple(current))
            current = []
        current.append(message)
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def by_turn(messages: Sequence[ContextMessage]) -> tuple[tuple[ContextMessage, ...], ...]:
    """按「轮」切分。**这是全模块唯一的轮定义。**"""
    turns: list[tuple[ContextMessage, ...]] = []
    current: list[ContextMessage] = []
    for message in messages:
        # 遇到新的 user 就**先收口上一轮**；开头的非 user 消息（若有）自成一轮，
        # 而不是被丢掉 —— 丢掉它们同样是静默丢内容。
        if message.role == "user" and current:
            turns.append(tuple(current))
            current = []
        current.append(message)
    if current:
        turns.append(tuple(current))
    return tuple(turns)


def flatten(turns: Sequence[Sequence[ContextMessage]]) -> tuple[ContextMessage, ...]:
    return tuple(message for turn in turns for message in turn)


def split_tail(
    turns: Sequence[Sequence[ContextMessage]], keep_recent: int
) -> tuple[tuple[tuple[ContextMessage, ...], ...], tuple[tuple[ContextMessage, ...], ...]]:
    """把轮分成 ``(可压缩的头, 不动的尾)``。

    **尾部不动**是刻意的（`MC-2`）：最近几轮是模型的"工作记忆"，
    压它等于让模型忘记自己正在做什么。
    """
    if keep_recent <= 0:
        return tuple(turns), ()
    cut = max(0, len(turns) - keep_recent)
    return tuple(turns[:cut]), tuple(turns[cut:])


def fingerprint(messages: Sequence[ContextMessage]) -> str:
    """内容指纹。**取内容而不是消息 id** —— 同一条消息被编辑后应当重新压缩。"""
    digest = hashlib.sha256()
    for message in messages:
        digest.update(message.role.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(message.content.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def validate_pairing(messages: Sequence[ContextMessage]) -> tuple[str, ...]:
    """检查工具调用与结果是否配对完整。**返回问题列表，空表示合法。**

    这是 `FR-C-14` 的守门人：产出之前过一遍，把"切在配对中间"这类问题
    在**本地**拦下来，而不是等厂商 400。
    """
    problems: list[str] = []
    pending: dict[str, int] = {}

    for index, message in enumerate(messages):
        if message.role == "assistant":
            for call_id in message.tool_call_ids:
                pending[call_id] = index
        elif message.role == "tool":
            call_id = message.tool_call_id
            if not call_id:
                problems.append(f"第 {index} 条是 tool 结果，但没有 tool_call_id")
            elif call_id not in pending:
                problems.append(
                    f"第 {index} 条是 tool 结果（id={call_id}），"
                    "但它对应的工具调用不在结果里 —— **配对被切断了**"
                )
            else:
                pending.pop(call_id, None)

    for call_id, index in pending.items():
        problems.append(
            f"第 {index} 条的助手消息发起了工具调用 {call_id}，但结果不在结果里 —— "
            "**配对被切断了**"
        )
    return tuple(problems)

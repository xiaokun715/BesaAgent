"""构造「发给模型的消息」的入口。

## 为什么这里需要一组构造函数

``Gateway.chat()`` 的形参是 ``messages: Sequence[Message]``，而 ``Message`` 是
**provider 的类型**。同时 B-1 要求「除 ``src/gateway/`` 外无任何模块直接 import
``provider``」。

于是任何要发起对话的上层（``src/agent``、``apps/*``）都会撞上同一个死胡同：

    要发消息 → 得构造 Message → 得 import provider → 被禁止。

出路与 ``gateway.tools.tool_spec`` 完全同构：**让 gateway 交出构造函数**。
它本来就 import provider，而 ``Message`` 正是它自己 API 的形参。

## 为什么是几个具名函数，而不是把 `Message` 直接再导出

两者都可以，但**具名函数把「常用形态」固化了**：

- ``assistant_tool_calls(...)`` 一次性把「assistant 消息 + 它发起的工具调用」配对构造 ——
  这两者在协议里**必须一起出现**，分开构造就有机会只写一个，
  而那会得到一个厂商看不懂的消息（或更糟：看起来能跑，但模型收不到工具上下文）；
- ``tool_result(...)`` 强制你给出 ``tool_call_id`` —— 少了它，多数厂商直接 400，
  且报错说的是「消息顺序不对」，指不到真正缺的那个字段。

**同时仍然再导出 ``Message``**（``from gateway.messages import Message``）——
因为类型检查与自定义形态仍然需要它，而那个需求不该逼人 import provider。
"""

from __future__ import annotations

from collections.abc import Sequence

from provider.types import Message, ToolCall, message_text

from gateway.tools import ToolCallView

__all__ = [
    "Message",
    "ToolCall",
    "assistant",
    "assistant_for_calls",
    "message_text",
    "system",
    "tool_result",
    "user",
]


def system(text: str) -> Message:
    """系统消息。**一个请求里通常只该有一条**，且放在最前面。"""
    return Message.text("system", text)


def user(text: str) -> Message:
    """用户消息。"""
    return Message.text("user", text)


def assistant(text: str = "", *, tool_calls: Sequence[ToolCall] = ()) -> Message:
    """助手消息。

    ⚠ **有工具调用时正文可以为空** —— 那是模型「只调工具、不说话」的正常形态。
    适配器的本地校验专门放行了这一种（见 ``provider.base.validate_messages``）。
    """
    return Message(role="assistant", content=text, tool_calls=tuple(tool_calls))


def assistant_for_calls(calls: Sequence[ToolCallView], *, text: str = "") -> Message:
    """把「从 gateway 结果里读出的工具调用」构造成一条助手消息。

    **收 ``ToolCallView`` 而不是 ``ToolCall``**：后者是 provider 的类型，
    上层构造不出来（那正是 ``tool_calls_of`` 存在的原因）。这里做的是反向的一步 ——
    把上层手里的本地类型变回协议要的形状。

    **单独一个函数是因为这两者在协议里必须成对**：随后的 ``tool_result``
    要靠 ``tool_call_id`` 指回它们。只构造其中一个，厂商那边要么 400、
    要么把工具结果当成一条孤立的用户消息。
    """
    if not calls:
        raise ValueError("assistant_for_calls 至少要有一个工具调用；只有正文时请用 assistant()")
    return Message(
        role="assistant",
        content=text,
        tool_calls=tuple(
            ToolCall(
                id=call.id,
                name=call.name,
                arguments_raw=call.arguments_raw,
                arguments=dict(call.arguments),
            )
            for call in calls
        ),
    )


def tool_result(tool_call_id: str, content: str) -> Message:
    """回填一次工具执行的结果。

    ``tool_call_id`` **必填**：它是把结果配回那次调用的唯一线索 ——
    少了它，多轮工具调用会整体错位，而厂商的报错通常只说「消息顺序不对」。
    """
    if not tool_call_id:
        raise ValueError(
            "tool_result 必须给出 tool_call_id —— 它是把结果配回那次调用的唯一线索"
        )
    return Message.tool_result(tool_call_id, content)

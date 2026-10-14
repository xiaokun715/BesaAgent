"""把块组装成**合法的消息序列**（`FR-C-14`）。

**这是所有策略的共同底线**，所以它单独一个文件、且排在压缩之后先做完：
先把它钉死，后面加策略时就不会各自踩配对问题。

本地能拦的都要拦掉。厂商的 400 报文通常是「第 N 项参数非法」——
既不说是哪条消息，也不说是哪个字段，而上下文是**动态拼出来的**，
出问题时调用方自己也不知道拼成了什么（与 ``provider.base.validate_messages`` 同一条理由）。

**消息一律经 ``gateway.messages`` 构造**：本模块不许 import ``provider``（`DC-3`）。
"""

from __future__ import annotations

from collections.abc import Sequence

from gateway.messages import (
    Message,
    assistant,
    assistant_for_calls,
    message_text,
    system as system_message,
    tool_result,
    user,
)
from gateway.tools import ToolCallView

from context.types import ContextBlock, ContextMessage, ToolCallRef

__all__ = ["build_messages", "from_message", "rebuild"]

#: 历史里若出现 system 消息，把它**并进置顶的那一条**，而不是再发一条。
#: 一个请求里只该有一条 system（``gateway.messages.system`` 的既有约定）。
_SYSTEM_JOIN = "\n\n"


def build_messages(
    *,
    system: str,
    blocks: Sequence[ContextBlock],
    current_input: str,
) -> tuple[Message, ...]:
    """组装。

    Args:
        system: system 提示。与历史里的 system 合并后置顶。
        blocks: **已经按组装顺序排好**的块（见 ``selector.ASSEMBLY_ORDER``）。
        current_input: 当前这一轮的用户输入。**永远排在最后**（`MC-12`）。

    Raises:
        ValueError: 当前输入为空 —— 这是**编程错误**（没有任务的上下文没有意义），
            与 ``validate_messages`` 把"空消息"当编程错误是同一条纪律。
    """
    if not current_input.strip():
        raise ValueError(
            "current_input 为空：没有任务的上下文没有意义。"
            "调用方应当传入这一轮的用户输入或任务描述。"
        )

    leading: list[str] = [system] if system.strip() else []
    body, extra_system = rebuild(blocks, split_system=True)
    leading.extend(extra_system)

    if not leading:
        # 空 system 会让"本模块负责上下文"在某些厂商上变成"没有指令"。
        leading.append("你是一个软件测试助手。")

    return (system_message(_SYSTEM_JOIN.join(leading)), *body, user(current_input))


def rebuild(
    blocks: Sequence[ContextBlock], *, split_system: bool = False
) -> tuple[list[Message], list[str]] | tuple[Message, ...]:
    """把块还原成消息序列（**不含额外的 system 与当前输入**）。

    ``build_messages`` 用它，**运行内压缩**也用它（`docs/agent` 的 `AQ-1`）：
    agent 的一次运行里没有"新的 user 输入"，有的只是「同一批消息压过之后的样子」。

    Args:
        split_system: 为真时返回 ``(消息, 历史里的 system 文本)``，
            让调用方把 system 合并到置顶那一条；否则历史 system 会被丢掉。
    """
    body: list[Message] = []
    systems: list[str] = []

    for block in blocks:
        message = block.message
        if message is None:
            # 压缩产物 / 记忆注入 —— 不是"某个人说的一句话"，而是**背景**。
            # 用 user 角色并把性质写进结构化标记（`MC-16`）：
            # 用 assistant 会和随后的原文形成连续同角色，部分厂商直接 400。
            body.append(user(block.to_model_text()))
            continue

        role = message.role
        if role == "system":
            if message.content.strip():
                systems.append(message.content)
            continue
        if role == "user":
            if message.content.strip():
                body.append(user(message.content))
            continue
        if role == "assistant":
            if message.tool_calls:
                body.append(assistant_for_calls(_views(message.tool_calls), text=message.content or ""))
            elif message.content.strip():
                body.append(assistant(message.content))
            # 空且无工具调用的 assistant 不携带任何信息，跳过 —— 它不是"内容"
            continue
        if role == "tool":
            if message.tool_call_id:
                body.append(tool_result(message.tool_call_id, message.content))
            else:
                # 配对 id 缺失：配对校验本该先拦下它。真到这一步**不能静默丢** ——
                # 把它当成一条普通输入带出去，让排障时看得见。
                body.append(user(f"[工具结果（缺少配对 id）] {message.content}"))
            continue

    if split_system:
        return body, systems
    return tuple(body)


def from_message(message: Message) -> ContextMessage:
    """``gateway.messages.Message`` → 本模块的 :class:`ContextMessage`。

    **运行内压缩需要它**（`AQ-1`）：执行循环里的消息是厂商形状的，
    而压缩器只认本模块的形状。转换收在这里，因为它与 ``rebuild`` 是一对 ——
    分开写迟早会对不上（比如一边记了工具调用、另一边没记）。

    **用 ``getattr`` 读工具调用**：它们是 ``provider`` 的类型，
    而本模块不许 import provider（`DC-3`）—— 形状是稳定的（id/name/arguments/arguments_raw）。
    """
    calls = tuple(
        ToolCallRef(
            id=str(getattr(call, "id", "")),
            name=str(getattr(call, "name", "")),
            arguments=dict(getattr(call, "arguments", {}) or {}),
            arguments_raw=str(getattr(call, "arguments_raw", "")),
        )
        for call in (getattr(message, "tool_calls", ()) or ())
    )
    return ContextMessage(
        role=getattr(message, "role", "user"),
        content=message_text(message),
        tool_calls=calls,
        tool_call_id=getattr(message, "tool_call_id", None),
        name=getattr(message, "name", None),
    )


def _views(calls: Sequence[object]) -> tuple[ToolCallView, ...]:
    """本模块的 ``ToolCallRef`` → gateway 的 ``ToolCallView``（**唯一的一处转换**）。"""
    return tuple(
        ToolCallView(
            id=call.id,  # type: ignore[attr-defined]
            name=call.name,  # type: ignore[attr-defined]
            arguments=dict(getattr(call, "arguments", {}) or {}),
            arguments_raw=getattr(call, "arguments_raw", ""),
        )
        for call in calls
    )

"""``src/context`` 的类型载体。

**这里的东西不依赖任何厂商协议**：消息用本模块自己的 :class:`ContextMessage`
（不是 ``provider.types.Message``）—— 因为 ``src/context`` 不许 import ``provider``
（`NFR-C-01`，且 `DC-3` 刚把 context 补进那条契约）。
厂商消息的构造发生在 :mod:`context.builder`，经 ``gateway.messages``。

**几个刻意多出来的区分**（合并掉它们会让下面这些问题再也答不出来）：

==========================  ==================================================
``Segment``                 一段内容**为什么**在上下文里（决定预算怎么砍）
``CompressionLevel``        它是原文还是被压过的（决定模型该不该把它当原文）
``Provenance``              「这一段覆盖了哪些原始消息」（回捞的唯一钩子）
``BudgetPlan.dropped``      砍掉了什么（**不静默丢内容**是 `NFR-C-08`）
==========================  ==================================================
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "COMPRESSION_LEVELS",
    "SEGMENTS",
    "STRATEGY_NAMES",
    "BudgetPlan",
    "ChatModel",
    "CompressionLevel",
    "ContextBlock",
    "ContextMessage",
    "LedgerEntry",
    "Provenance",
    "Role",
    "Segment",
    "StrategyName",
    "ToolCallRef",
]

#: 消息角色。与厂商角色同名，但**是本模块自己的字面量**。
Role = Literal["system", "user", "assistant", "tool"]

#: 一段内容**为什么**在上下文里。它同时决定预算不够时先砍谁。
#:
#: ``system`` / ``current`` 是**不可动**的两段 —— 它们承载任务本身（`MC-12`）；
#: 砍它们等于"预算紧的时候先把任务砍了"。
Segment = Literal[
    "system",
    "current",
    "window",
    "transition",
    "session_summary",
    "long_term",
]

SEGMENTS: tuple[Segment, ...] = (
    "system", "current", "window", "transition", "session_summary", "long_term",
)

#: 预算不够时**从后往前**砍（`FR-C-13`）。
#:
#: 顺序是契约的一部分，不是实现细节 —— 它决定了"预算紧时模型先失去什么"。
DROP_ORDER: tuple[Segment, ...] = (
    "long_term", "session_summary", "transition", "window",
)

#: 多级压缩的级别（`FR-C-03`）。名字与顺序是契约，不许出现"中间态"。
#:
#: ``L0`` 原文 / ``L1`` 去噪（零 LLM）/ ``L2`` 摘要 / ``L3`` 要点。
CompressionLevel = Literal["L0", "L1", "L2", "L3"]

COMPRESSION_LEVELS: tuple[CompressionLevel, ...] = ("L0", "L1", "L2", "L3")

#: 上下文管理策略名（`FR-C-01`）。
StrategyName = Literal["linear", "tiered"]

STRATEGY_NAMES: tuple[StrategyName, ...] = ("linear", "tiered")


@runtime_checkable
class ChatModel(Protocol):
    """一次文本补全。**压缩摘要走它**。

    与 ``memory.types.ChatModel`` **形状相同**（刻意保持）：于是组合根用
    **同一个适配器**（把 ``Gateway.chat`` 包成这个形状）同时喂给记忆与上下文，
    而不必写两份。两边都只认协议，谁都不认识 ``Gateway``。
    """

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class ToolCallRef:
    """一次工具调用的**最小还原信息**。

    **为什么要 ``arguments_raw``**：重建 assistant 消息时必须把它原样还给厂商 ——
    只留解析后的 ``arguments`` 会在「模型给的 JSON 不合法」时丢掉排障线索，
    而那种情况正是最需要看原文的时候（与 ``gateway.tools.ToolCallView`` 同一条理由）。
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    arguments_raw: str = ""


@dataclass(frozen=True)
class ContextMessage:
    """一条历史消息的**本模块形态**。

    ``tool_calls`` 与 ``tool_call_id`` 是**配对校验的全部依据**（`FR-C-14`）：
    切分点必须保证「调用」与「结果」同进同出，否则厂商直接 400，
    而报错说的是"消息顺序不对"，指不到真正的问题。
    """

    role: Role
    content: str = ""
    #: 消息的稳定标识。**回捞与账本都靠它**（`FR-C-16`）。
    message_id: str = ""
    #: assistant 消息里发起的工具调用（可能多个）
    tool_calls: tuple[ToolCallRef, ...] = ()
    #: tool 消息回填的是哪一次调用
    tool_call_id: str | None = None
    name: str | None = None

    @property
    def is_tool_result(self) -> bool:
        return self.role == "tool"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def tool_call_ids(self) -> tuple[str, ...]:
        """发起的工具调用 id。配对校验与切分点都用它。"""
        return tuple(call.id for call in self.tool_calls)


@dataclass(frozen=True)
class Provenance:
    """一段内容的出处。**账本与回捞共用它**（`FR-C-05` / `FR-C-16`）。"""

    segment: Segment
    level: CompressionLevel = "L0"
    #: 这一段**覆盖了哪些原始消息**。压缩产物的回捞就靠它 ——
    #: 没有它，摘要就是终点而不是索引。
    message_ids: tuple[str, ...] = ()
    #: 长期记忆带过来的相关度（其余段为 ``None``）
    confidence: float | None = None
    note: str = ""

    @property
    def compacted(self) -> bool:
        return self.level != "L0"


@dataclass(frozen=True)
class ContextBlock:
    """组装的最小单位。``role`` 决定它最终以什么身份回到消息序列里。

    ``message`` 非空表示**这一块就是一条原始消息**（L0/L1），组装时按它的
    role / tool_call_id 原样还原 —— 摘要是**文字**，而工具调用与结果必须带着
    它们的 id 才能配对（`FR-C-14`）。这个字段是「原文」与「压缩产物」在同一个
    序列里共存的方式。
    """

    content: str
    role: Role = "assistant"
    provenance: Provenance = field(default_factory=lambda: Provenance(segment="window"))
    #: 原文块携带的原始消息（压缩产物为 ``None``）
    message: "ContextMessage | None" = None

    def to_model_text(self) -> str:
        """回灌给模型的文本。

        **被压过的内容必须带结构化标记**（`FR-C-06`）：模型会把自己摘要里
        「没提到」当成「当时没发生」，而标记让它知道自己在看的是索引而不是原文。

        标记是**可解析**的（``range=`` 里是原始消息 id），因为回捞要靠它。
        """
        if not self.provenance.compacted:
            return self.content
        span = ",".join(self.provenance.message_ids[:2])
        if len(self.provenance.message_ids) > 2:
            span += f",…共{len(self.provenance.message_ids)}条"
        return (
            f"<<<compacted level={self.provenance.level} range={span or '-'} "
            f"这是早期对话的压缩摘要，可能丢失细节>>>\n"
            f"{self.content}\n"
            f"<<<compacted end>>>"
        )


@dataclass(frozen=True)
class BudgetPlan:
    """一次请求的预算画像。**它同时是排障的入口**（`NFR-C-06`）。"""

    model_window: int
    available_input: int
    reserve_output: int = 0
    safety_margin: int = 0
    allocated: Mapping[Segment, int] = field(default_factory=dict)
    used: Mapping[Segment, int] = field(default_factory=dict)
    dropped: Mapping[Segment, int] = field(default_factory=dict)
    #: 连"不可动的两段"都放不下 —— **如实上报**，不静默截断（`FR-C-13`）
    insufficient: bool = False
    notes: tuple[str, ...] = ()

    @property
    def total_used(self) -> int:
        return sum(self.used.values())

    @property
    def total_dropped(self) -> int:
        return sum(self.dropped.values())

    def describe(self) -> str:
        """一行摘要，给日志与事件用。"""
        used = ", ".join(f"{k}={v}" for k, v in self.used.items() if v)
        dropped = ", ".join(f"{k}={v}" for k, v in self.dropped.items() if v)
        text = f"窗口 {self.model_window} → 可用输入 {self.available_input}；占用 {used or '无'}"
        if dropped:
            text += f"；**砍掉** {dropped}"
        if self.insufficient:
            text += "；⚠ 预算不足"
        return text


@dataclass(frozen=True)
class LedgerEntry:
    """一次压缩的账（`FR-C-05`）。

    **这是"当时到底有什么"唯一的事后依据** —— 摘要本身回答不了这个问题。
    """

    level: CompressionLevel
    segment: Segment
    message_ids: tuple[str, ...] = ()
    before_tokens: int = 0
    after_tokens: int = 0
    reason: str = ""
    cache_key: str = ""
    at: float = 0.0

    @property
    def saved(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

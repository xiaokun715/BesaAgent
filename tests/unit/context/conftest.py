"""``src/context`` 单测的公共夹具。

**LLM 用确定性的假实现**（`NFR-C-05`）：压缩的判定与级联**不该依赖真模型的输出** ——
否则测试只能断言"大概是压过了"，而这一层最需要钉住的恰恰是"压到第几级、压没压"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from foundation.clock import FakeClock

from context.budget import BudgetPolicy
from context.compact.levels import Compactor
from context.compact.policy import CompactPolicy
from context.compact.summarizer import Summarizer
from context.compressor import build_compactor
from context.manager import ContextManager, ContextPolicy
from context.resolver import MemoryResolver
from context.strategies import build_strategies
from context.types import ContextMessage, ToolCallRef

# --------------------------------------------------------------------------- #
# 假模型
# --------------------------------------------------------------------------- #


class FakeChat:
    """脚本化的补全。**记录调用次数** —— 级联的核心断言就是"调了几次"。"""

    def __init__(self, *, summary: str = "（摘要）关键决策：沿用 A 方案。", fail: bool = False) -> None:
        self.summary = summary
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        json_schema: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        self.calls.append((system[:8], prompt))
        if self.fail:
            raise RuntimeError("chat 故意失败")
        if "要点列表" in system:
            return "- 要点一\n- 要点二"
        return self.summary

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeGateway:
    """只提供 ``context_window`` —— 上下文层向 gateway 要的就是这一个数（`DC-1`）。"""

    def __init__(self, window: int | None = 8192) -> None:
        self._window = window
        self.queries: list[str] = []

    def context_window(self, alias: str) -> int | None:
        self.queries.append(alias)
        return self._window


class FakeMemory:
    """假的长期记忆服务。**只实现 context 用到的那两个方法。**"""

    def __init__(self, *, enabled: bool = True, memories: Sequence[dict[str, Any]] = ()) -> None:
        self.enabled = enabled
        self.memories = list(memories)
        self.recalls: list[str] = []
        self.remembered: list[Any] = []

    async def recall(self, query: str, *, ctx: Any = None, kinds: Any = None, top_k: Any = None):
        self.recalls.append(query)
        return [_FakeHit(**m) for m in self.memories[: (top_k or len(self.memories))]]

    async def remember(self, conversation: Any, *, source: Any = None, ctx: Any = None) -> Any:
        self.remembered.append((conversation, source))
        return object()


class _FakeItem:
    def __init__(self, content: str, kind: str = "fact") -> None:
        self.content = content
        self.kind = kind

    def to_model_text(self) -> str:
        return f"<<<不可信记忆 来源=agent>>>\n{self.content}\n<<<不可信记忆结束>>>"


class _FakeHit:
    def __init__(self, content: str, kind: str = "fact", score: float = 0.9) -> None:
        self.item = _FakeItem(content, kind)
        self.score = score


# --------------------------------------------------------------------------- #
# 构造辅助
# --------------------------------------------------------------------------- #


def turn(index: int, *, size: int = 40, tool: bool = False) -> tuple[ContextMessage, ...]:
    """造一轮对话。``tool=True`` 时带一次工具调用与它的结果（用于配对测试）。"""
    messages: list[ContextMessage] = [
        ContextMessage(role="user", content=f"第{index}轮：请分析需求 {index}", message_id=f"u{index}")
    ]
    if tool:
        call_id = f"call{index}"
        messages.append(
            ContextMessage(
                role="assistant",
                content="",
                message_id=f"a{index}",
                tool_calls=(ToolCallRef(id=call_id, name="read", arguments={"path": "x"}, arguments_raw='{"path":"x"}'),),
            )
        )
        messages.append(
            ContextMessage(role="tool", content="内容" * size, message_id=f"t{index}", tool_call_id=call_id)
        )
    else:
        messages.append(
            ContextMessage(role="assistant", content="结论：" + "内容" * size, message_id=f"a{index}")
        )
    return tuple(messages)


def history(turns: int, *, tool: bool = False, size: int = 40) -> tuple[ContextMessage, ...]:
    out: list[ContextMessage] = []
    for index in range(turns):
        out.extend(turn(index, size=size, tool=tool))
    return tuple(out)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_767_225_600)


@pytest.fixture
def chat() -> FakeChat:
    return FakeChat()


@pytest.fixture
def compactor(chat, clock) -> Compactor:
    return build_compactor(chat=chat, cfg={"keep_recent_turns": 2, "max_level": "L2"}, clock=clock)


def make_manager(
    *,
    chat: FakeChat | None = None,
    gateway: Any | None = None,
    memory: Any | None = None,
    clock: Any | None = None,
    strategy: str = "linear",
    context_cfg: Mapping[str, Any] | None = None,
    compactor: Compactor | None = None,
) -> ContextManager:
    """装一个可用的 ``ContextManager``。"""
    cfg = dict(context_cfg or {})
    compact_cfg = {"keep_recent_turns": 2, "max_level": "L2"}
    compact_cfg.update(cfg.get("linear", {}).get("compact", {}) if isinstance(cfg.get("linear"), Mapping) else {})

    comp = compactor or build_compactor(chat=chat, cfg=compact_cfg, clock=clock)
    resolver = MemoryResolver(memory)
    strategies = build_strategies(compactor=comp, resolver=resolver, tiered_cfg=cfg.get("tiered"))
    policy = ContextPolicy.from_config({"strategy": strategy, **cfg})
    return ContextManager(
        strategies=strategies,
        policy=policy,
        gateway=gateway or FakeGateway(),
        resolver=resolver,
        clock=clock,
    )


def summarizer_for(chat: FakeChat, *, prompt_version: str = "v1") -> Summarizer:
    return Summarizer(chat, max_tokens=64, prompt_version=prompt_version)


def compact_policy(**overrides: Any) -> CompactPolicy:
    data = {"keep_recent_turns": 2}
    data.update(overrides)
    return CompactPolicy.from_config(data)


def budget_policy(**overrides: Any) -> BudgetPolicy:
    return BudgetPolicy.from_config(overrides)

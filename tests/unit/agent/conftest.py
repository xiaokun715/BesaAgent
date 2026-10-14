"""``src/agent`` 单测的公共夹具。

**为什么这些假把式要做成这样**：运行时层最需要钉住的性质是
「**调了几次模型 / 撞的是哪个上界 / 停下来时给了什么理由**」——
它们都不该依赖真模型。所以 :class:`FakeGateway` 按脚本回话，
:class:`FakeTools` 只记账，而时钟永远是假的（`NFR-A-06`）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import pytest

from foundation.clock import FakeClock
from foundation.database import Database
from foundation.db import Base, create_engine

from agent.runtime import AgentPolicy, AgentRuntime, ContextRunPolicy, ResumePolicy
from agent.stages import build_stages
from agent.router import StageRouter

# 建表：checkpoint 与 tool_execution 都要注册到 Base.metadata
import repo.models  # noqa: F401  (E402)


class FakeCall:
    """模型要求的一次工具调用（形状与 ``tool_calls_of`` 读的一致）。"""

    def __init__(self, name: str = "read", call_id: str = "c1", arguments: Mapping[str, Any] | None = None) -> None:
        self.id = call_id
        self.name = name
        self.arguments = dict(arguments or {})
        self.arguments_raw = "{}"


class FakeResponse:
    def __init__(self, content: str = "", tool_calls: Sequence[FakeCall] = ()) -> None:
        self.content = content
        self.tool_calls = tuple(tool_calls)


class FakeCost:
    """``amount=None`` 表示**价格未知**（不是 0 元，`FR-G-09`）。"""

    def __init__(self, amount: Decimal | None = Decimal("0")) -> None:
        self.amount = amount
        self.currency = "CNY" if amount is not None else ""


class FakeGatewayResult:
    """形状与 ``gateway.types.GatewayResult`` 对齐 —— **``content`` 是 property**。"""

    def __init__(self, response: FakeResponse, cost: FakeCost | None = None) -> None:
        self.response = response
        self.cost = cost or FakeCost()
        self.attempts = ()
        self.degraded = False

    @property
    def content(self) -> str:
        return getattr(self.response, "content", "")

    @property
    def model(self) -> str:
        return "fake-model"


class FakeGateway:
    """按脚本回话。**用完之后重复最后一条** —— 免得用例要为每一步都排脚本。"""

    def __init__(self, replies: Sequence[FakeResponse] | None = None, *, window: int | None = 8192) -> None:
        self.replies = list(replies or [FakeResponse("做完了")])
        self.window = window
        self.calls: list[dict[str, Any]] = []

    async def chat(self, alias: str, messages: Sequence[Any], **kwargs: Any) -> FakeGatewayResult:
        self.calls.append({"alias": alias, "messages": list(messages), **kwargs})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return FakeGatewayResult(reply)

    def context_window(self, alias: str) -> int | None:
        return self.window


class FakeToolResult:
    def __init__(self, text: str = "工具结果", *, idem_key: str = "k1") -> None:
        self._text = text
        self.idem_key = idem_key
        self.outcome = "executed"

    def to_model_text(self) -> str:
        return self._text


class FakeTools:
    """只记账的工具执行器。"""

    def __init__(self, registry: Any | None = None) -> None:
        self.registry = registry if registry is not None else _FakeRegistry()
        self.invocations: list[Any] = []

    async def invoke(self, invocation: Any) -> FakeToolResult:
        self.invocations.append(invocation)
        return FakeToolResult(f"{invocation.tool_name} 的结果", idem_key=f"key-{len(self.invocations)}")


class _FakeRegistry:
    def __init__(self, names: Sequence[str] = ("read", "write", "bash")) -> None:
        self._names = tuple(names)

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def __len__(self) -> int:
        return len(self._names)


class FakeCatalog:
    async def select(self, **kwargs: Any):
        return ()


@pytest.fixture
async def db():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.aclose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_767_225_600)


def make_runtime(
    *,
    gateway: FakeGateway | None = None,
    tools: FakeTools | None = None,
    database: Database | None = None,
    context: Any | None = None,
    clock: Any | None = None,
    policy: AgentPolicy | None = None,
    routing: Mapping[str, Any] | None = None,
) -> AgentRuntime:
    """装一个可用的 ``AgentRuntime``（用**真的** AgentExecutor + 假的 gateway/tools）。"""
    from agent.executor import AgentExecutor

    gw = gateway or FakeGateway()
    tool_executor = tools or FakeTools()
    executor = AgentExecutor(
        gateway=gw,
        tools=tool_executor,
        catalog=FakeCatalog(),
        database=database,
        max_steps=(policy.limits.max_steps if policy else 20),
        clock=clock,
    )
    stages = build_stages(None)
    router = StageRouter(
        stages,
        on_unknown=str((routing or {}).get("on_unknown", "ask")),
        default_stage=str((routing or {}).get("default_stage", "")),
    )
    return AgentRuntime(
        executor=executor,
        stages=stages,
        router=router,
        context=context,
        database=database,
        policy=policy or AgentPolicy(),
        clock=clock,
    )


def policy(**overrides: Any) -> AgentPolicy:
    """构造一个可改的 AgentPolicy。"""
    limits = overrides.pop("limits", {})
    resume = overrides.pop("resume", {})
    ctx = overrides.pop("context", {})
    from agent.runtime import AgentLimits

    return AgentPolicy(
        alias=overrides.pop("alias", "runtime.default"),
        limits=AgentLimits.from_config(limits),
        resume=ResumePolicy.from_config(resume),
        context=ContextRunPolicy.from_config(ctx),
        emit_events=overrides.pop("emit_events", True),
    )

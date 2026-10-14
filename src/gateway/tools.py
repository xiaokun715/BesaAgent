"""工具定义在 **gateway 边界上**的形状。

## 为什么这个文件必须存在

``Gateway.chat()`` 的形参是 ``tools: Sequence[ToolSpec]``，而 ``ToolSpec`` 是
**provider 的类型**。同时 ``FR-G-01``（验收点 B-1）要求：

> 全仓库检索，**除 ``src/gateway/`` 外无任何模块**直接 import ``src/provider/``。

**这两条放在一起，调用方就走进了一个死胡同**：想用工具调用，就得构造一个
``ToolSpec``；而构造它就得 import provider —— 那是被禁止的。

所以「怎么造一个 ToolSpec」必须由 **gateway 这一侧**提供。
它本来就 import provider（它是 provider 的唯一消费者），
而这个类型正是**它自己 API 的形参** —— 交给别人去造才是奇怪的。

## 这也是「上层不必认识 provider」的一个样板

``src/composition/tools.py`` 用它把 ``ToolDefinition`` 转成厂商形状，
**全程不 import provider**，于是 B-1 那条机械约束**不需要开任何豁免**。

这与 ``GatewayResult.content`` 用 ``getattr`` 而不是类型注解是同一个目的
（让上层不必 import provider.types），但手法更干净：**给出一个构造函数，
而不是让上层去做鸭子类型访问**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from provider.types import ToolSpec

from gateway.types import GatewayResult

__all__ = [
    "ToolCallView",
    "ToolSpec",
    "ToolLike",
    "to_tool_specs",
    "tool_calls_of",
    "tool_spec",
]


@runtime_checkable
class ToolLike(Protocol):
    """「看起来像一个工具定义」需要的最小形状。

    **用结构协议而不是 import ``tool.types.ToolDefinition``**：那样 gateway
    就要依赖 tool 层，而 gateway 比 tool 低半层 —— 一个为了三个字段的依赖，
    会把两层绑在一起。鸭子类型在这里刚好合适：我们**只读三个字段**，
    不需要知道它是什么类型。
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


def to_tool_specs(definitions: Iterable[ToolLike]) -> tuple[ToolSpec, ...]:
    """把工具定义批量转成厂商形状。

    **这个转换只能在这里做**（`DT-5` / `RT-13`）：``Gateway.chat()`` 的形参是
    ``Sequence[ToolSpec]``，而 ``ToolSpec`` 是 provider 的类型；
    除了 gateway，谁都不许 import provider（B-1）。

    转换本身很薄（三个字段同名），**薄是对的** ——
    它证明了两边的契约形状本来就一致。一旦要在这里做映射表，
    说明工具层的定义开始泄漏厂商细节了，那是该停下来看的信号。
    """
    return tuple(
        tool_spec(
            name=item.name,
            description=item.description,
            parameters=item.parameters,
        )
        for item in definitions
    )


@dataclass(frozen=True)
class ToolCallView:
    """模型要求的一次工具调用 —— **上层的本地形态**。

    ``arguments`` 是**解析后**的映射，``arguments_raw`` 是厂商给的**原始字符串**。
    两个都留：原文可能不是合法 JSON（模型幻觉），那时解析结果为空而原文还在 ——
    只留一个，要么丢排障线索，要么逼每个消费者自己再解析一遍。
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    arguments_raw: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolCallView 必须有工具名")


def tool_calls_of(result: GatewayResult) -> tuple[ToolCallView, ...]:
    """从一次 gateway 调用的结果里取出「模型要求调用的工具」。

    **这是上层读取工具调用的唯一入口** —— 于是上层不必 import ``provider.types``
    （B-1 禁止），也不必对 ``response`` 做鸭子类型访问。

    返回 ``()`` 有三种正常情形，**它们不需要被区分**：
    模型这一轮没想调工具（就是要给答案了）、这是向量化调用（压根没有工具这回事）、
    以及工具调用字段为空。真正要区分它们的是 ``response.content`` 有没有内容。
    """
    response = result.response
    calls = getattr(response, "tool_calls", None) or ()
    return tuple(
        ToolCallView(
            id=str(getattr(call, "id", "")),
            name=str(getattr(call, "name", "")),
            arguments=dict(getattr(call, "arguments", None) or {}),
            arguments_raw=str(getattr(call, "arguments_raw", "")),
        )
        for call in calls
    )


def tool_spec(
    *,
    name: str,
    description: str = "",
    parameters: Mapping[str, Any] | None = None,
) -> ToolSpec:
    """构造一个可以传给 ``gateway.chat(tools=...)`` 的工具定义。

    Args:
        name: 工具名。**模型看到的就是它**，所以必须稳定 —— 改名等于换一个工具。
        description: 给模型看的说明。写得含糊会直接影响它选不选这个工具。
        parameters: 参数的 JSON Schema。**本函数不校验它** ——
            校验发生在工具执行前（``src/tool`` 的 schema 校验那一关），
            那里才有「这个工具到底要什么」的事实。
    """
    if not name.strip():
        raise ValueError("工具名不能为空")
    return ToolSpec(
        name=name,
        description=description,
        parameters=dict(parameters or {}),
    )

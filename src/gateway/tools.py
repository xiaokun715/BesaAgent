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

from collections.abc import Mapping
from typing import Any

from provider.types import ToolSpec

__all__ = ["ToolSpec", "tool_spec"]


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

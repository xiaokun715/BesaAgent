"""工具契约与执行上下文。

**``Tool`` 是纯行为，``ToolDefinition`` 是纯数据**（在 ``types.py``）。
分开是因为它们的消费者不同：组合根要把**定义**转成厂商的 ``ToolSpec`` 发给模型，
而它不该拿到**执行入口**；执行入口只给 ``executor``。

**``ToolContext`` 刻意不含 Redis / 数据库句柄**：工具实现不该自己做幂等，
它甚至不该知道幂等的存在 —— 那是 ``executor`` 的事。
让工具拿到那些句柄，就等于允许每个工具各写一套去重逻辑，
而「数次数的地方只有一处」这条纪律（``CallBudget`` 那一课的同一个道理）会立刻失效。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from foundation.clock import Clock, SystemClock
from tool.types import SideEffect, ToolDefinition, ToolResult

__all__ = ["Tool", "ToolContext", "truncate"]


@dataclass(frozen=True)
class ToolContext:
    """一次执行的上下文。

    ``allowed_paths`` 是**已解析成绝对路径**的授权范围 ——
    由 ``permission.py`` 在调用工具之前算好并解析完（含符号链接），
    而不是让每个工具自己去判断「这个路径算不算在范围内」。
    后者的问题是：判断逻辑会被复制 N 份，而其中一份漏了 ``..`` 或符号链接，
    整个范围就形同虚设。
    """

    scope: str = ""
    allowed_paths: tuple[Path, ...] = ()
    trace_id: str = ""
    session_id: str | None = None
    caller: str | None = None
    #: 单次输出上限（字节与行数）。工具必须遵守 —— `NFR-T-05`
    max_output_bytes: int = 262_144
    max_output_lines: int = 2_000
    timeout_s: float = 60.0
    clock: Clock = field(default_factory=SystemClock)

    def within(self, path: Path) -> bool:
        """``path``（已解析）是否落在授权范围内。**范围为空即拒绝**（默认拒绝）。"""
        if not self.allowed_paths:
            return False
        return any(
            path == allowed or allowed in path.parents for allowed in self.allowed_paths
        )


def truncate(text: str, *, max_bytes: int, max_lines: int) -> tuple[str, bool]:
    """把输出截到上限内，返回 ``(截断后的文本, 是否截断过)``。

    **先按行截、再按字节截**，两次都可能截断，所以只要有一次生效就返回 ``True``。

    **调用方必须把 ``truncated`` 传出去**（放进 :class:`ToolResult`）——
    截断而不标注，会让模型基于不完整信息做判断，而它自己不知道。
    这比报错更糟：报错它会换个做法，静默截断它会基于错的输入继续推理。
    """
    truncated = False

    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    result = "".join(lines)

    encoded = result.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        # 按字节截断时必须**回退到字符边界** —— 直接切字节会把一个多字节字符
        # 劈成两半，得到一串无法解码的字节，而错误会出现在很远的地方（写文件/编码时）。
        result = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    return result, truncated


class Tool(ABC):
    """一个可被模型调用的工具。

    子类只要声明四个类属性并实现 :meth:`run`。**不要覆写 :meth:`definition`** ——
    它是那四个属性的投影，覆写会让两者有机会不一致。
    """

    #: 工具名。模型看到的就是它，所以必须是稳定的（改名等于换一个工具）
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    #: 参数 JSON Schema
    parameters: ClassVar[Mapping[str, Any]] = {}
    #: **副作用等级**（`DT-4`）。它同时决定权限策略与幂等降级行为 ——
    #: 声明错了会在两处同时出错，所以新增工具时这是最该想清楚的一个。
    side_effect: ClassVar[SideEffect] = "read"

    #: **类别**（`FR-T-13`）。用于「一轮只暴露这一类」的静态收窄。
    #:
    #: 默认是 ``infra``（读文件、跑命令这类通用工具）。业务侧的工具应当声明成
    #: 它所属的测试阶段（``requirement`` / ``test_design`` / ``test_case`` …）——
    #: 那七个阶段天然正交，是**零成本**的收窄：agent 在某个阶段只该看见它那类工具。
    category: ClassVar[str] = "infra"

    def definition(self) -> ToolDefinition:
        """给模型看的定义。由属性投影而来，**保证不会与实现不一致**。

        **刻意是实例方法而不是类方法**：类方法只能读到类属性，
        于是「同一类工具的两个实例带不同配置」就做不到了 ——
        而那是个真实需求（两个作用域不同的 ``read``、两个指向不同服务的 HTTP 工具）。
        注册进注册表的是**实例**，定义自然该跟着实例走。

        子类**不要覆写它** —— 覆写之后定义与实现就有机会不一致，
        而那份不一致是静默的（模型看到的与执行的不是一回事）。
        """
        if not self.name:
            raise ValueError(f"{type(self).__name__} 没有声明 name")
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=dict(self.parameters),
            side_effect=self.side_effect,
            category=self.category,
        )

    @abstractmethod
    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        """执行。

        **实现方不必、也不该处理幂等** —— ``executor`` 会在调用之前解决它。
        这里只回答「假设这是第一次执行，结果是什么」。

        **输出必须经 :func:`truncate` 处理并把 ``truncated`` 带出去。**
        """
        ...

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, side_effect={self.side_effect!r})"


def require(args: Mapping[str, Any], key: str, *, tool: str) -> Any:
    """取一个必需参数，缺了就报错。

    错误信息里带上工具名与已给的键 —— 缺参数时最想知道的是「我到底给了什么」。
    """
    if key not in args:
        given = ", ".join(sorted(args)) or "（一个都没有）"
        raise ValueError(f"{tool} 缺少必需参数 {key!r}；已给参数：{given}")
    return args[key]


def require_text(args: Mapping[str, Any], key: str, *, tool: str) -> str:
    value = require(args, key, tool=tool)
    text = str(value)
    if not text.strip():
        raise ValueError(f"{tool} 的参数 {key!r} 不能为空")
    return text

"""工具注册表 —— 注册、查找、以及「该给模型看哪些工具」。

**注册表不认识幂等、权限、Redis、数据库** —— 它是纯内存的名字到实例的映射。
这样它可以在任何环境里构造（包括单测），而装配的事归组合根。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence

from tool.base import Tool
from tool.types import SideEffect, ToolDefinition

__all__ = ["ToolRegistry", "UnknownToolError"]


class UnknownToolError(LookupError):
    """请求了一个未注册的工具。

    **错误信息里列出全部可用名** —— 与 ``gateway`` 的逻辑名寻址（``FR-G-02``）
    是同一条纪律：拼错是最高频的错误，而只说「未找到」会让人去翻文档。
    在 agent 场景里这条更值：工具名是**模型生成的**，
    它拼错时最需要的就是「你到底有哪些工具」。
    """

    def __init__(self, name: str, known: Sequence[str]) -> None:
        self.name = name
        self.known = tuple(known)
        listing = ", ".join(self.known) or "（一个都没有）"
        super().__init__(f"未知的工具 {name!r}；已注册的工具：{listing}")


class ToolRegistry:
    """工具的注册与查找。

    **重名是硬失败**：静默覆盖会让一个工具悄悄失效，
    而「从哪一刻开始失效的」查不出来 —— 两个同名的工具里，
    后注册的那个赢了，但调用方以为自己调的是前一个。
    """

    __slots__ = ("_tools",)

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    # ---------------------------------------------------------------- 注册
    def register(self, tool: Tool) -> None:
        """注册一个工具。

        Raises:
            ValueError: 名字为空，或与已注册的工具重名。
        """
        name = getattr(tool, "name", "")
        if not name:
            raise ValueError(f"{type(tool).__name__} 没有声明 name，无法注册")

        existing = self._tools.get(name)
        if existing is not None:
            raise ValueError(
                f"工具名 {name!r} 已被 {type(existing).__name__} 占用，"
                f"不能再注册 {type(tool).__name__}。\n"
                "重名是硬失败而不是覆盖：静默覆盖会让其中一个悄悄失效，"
                "而调用方以为自己调的是另一个 —— 且没有报错。"
            )
        self._tools[name] = tool

    # ---------------------------------------------------------------- 查找
    def get(self, name: str) -> Tool:
        """按名字取工具。

        Raises:
            UnknownToolError: 未注册。错误信息含全部可用名。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(name, self.names)
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools[name] for name in self.names)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # ---------------------------------------------------------------- 筛选
    def definitions(
        self,
        *,
        allow: Collection[SideEffect] | None = None,
        only: Collection[str] | None = None,
    ) -> tuple[ToolDefinition, ...]:
        """挑出「这一次该给模型看」的工具定义。

        **两件筛选是有实际意义的**：

        - ``allow``：把当前调用方**没权限用**的工具藏起来。
          给模型看见它用不了的工具，它会去调用然后被拒 —— 白费一轮往返，
          而且被拒的次数会进它自己的上下文，进一步干扰后面的决策。
        - ``only``：调用方显式点名要哪些（子集），用于「一个阶段只暴露它需要的工具」。

        两个条件都满足才收 —— 交集语义，而不是「有一个满足就算」。
        """
        chosen = []
        for name in self.names:
            if only is not None and name not in only:
                continue
            tool = self._tools[name]
            if allow is not None and tool.side_effect not in allow:
                continue
            chosen.append(tool.definition())
        return tuple(chosen)


def build_default_registry(*, enabled: Collection[str] | None = None) -> ToolRegistry:
    """按配置装配内置工具。

    ``enabled`` 来自 ``tool.enabled``（见需求说明书 §7）。**默认不含 ``bash``** ——
    它能做任何事，默认打开等于默认给 agent 一台无锁的机器（``DT-6``）。
    """
    # 延迟 import：让「只想拿契约做类型检查」的人不必拖上这些实现
    from tool.bash import BashTool
    from tool.grep import GrepTool
    from tool.read import ReadTool
    from tool.write import WriteTool

    builtin: dict[str, Tool] = {
        ReadTool.name: ReadTool(),
        GrepTool.name: GrepTool(),
        WriteTool.name: WriteTool(),
        BashTool.name: BashTool(),
    }

    if enabled is None:
        # 默认集：三个不 destructive 的。bash 必须显式打开。
        enabled = tuple(name for name in builtin if builtin[name].side_effect != "destructive")

    unknown = {str(name) for name in enabled} - set(builtin)
    if unknown:
        known = ", ".join(sorted(builtin))
        raise ValueError(f"未知的工具名：{', '.join(sorted(unknown))}；已知：{known}")

    return ToolRegistry(builtin[str(name)] for name in enabled)

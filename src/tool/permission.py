"""权限：按副作用等级与调用方范围判定，并算出这次执行的上下文。

**范围默认拒绝**（``C-T-1``）：没配就是不许访问，不是「默认允许、配了才限制」。
后者的问题是「忘了配」的默认行为恰好是**最危险**的那一种 ——
而配置漏配是常态。

**路径在这里解析成绝对路径**（含符号链接），而不是让每个工具自己判断。
判断逻辑复制 N 份，其中一份漏了 ``..`` 或符号链接，整个范围就形同虚设；
收在一处之后，工具只需要 :meth:`ToolContext.within`。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tool.base import ToolContext
from tool.types import SideEffect, ToolDefinition

__all__ = ["PermissionDecision", "PermissionPolicy", "SideEffectPolicy"]

#: 危险程度排序，用于「至少需要哪一级」这类判断
_SEVERITY: Mapping[str, int] = {"read": 0, "write": 1, "destructive": 2}


@dataclass(frozen=True)
class SideEffectPolicy:
    """某一档副作用等级的授权范围。**范围为空 = 拒绝**。"""

    paths: tuple[Path, ...] = ()

    @classmethod
    def from_config(cls, value: Any) -> SideEffectPolicy:
        items: Sequence[Any] = value if isinstance(value, (list, tuple)) else (value or ())
        resolved = tuple(Path(str(item)).expanduser().resolve() for item in items if str(item).strip())
        return cls(paths=resolved)

    @property
    def granted(self) -> bool:
        return bool(self.paths)


@dataclass(frozen=True)
class PermissionDecision:
    """能不能执行，以及**为什么**。

    ``reason`` 是给模型与排障用的：只说「无权限」会让人不知道该去加什么授权，
    而错误信息的质量直接决定这一轮是「模型自己改对」还是「人来查文档」。
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class PermissionPolicy:
    """授权策略。

    ``enabled`` 是「哪些等级可以用」，各等级的 ``scopes`` 是「在这个等级下能碰哪些路径」。
    两者是**与**的关系：等级没开就一定拒绝，等级开了但范围为空也拒绝。
    """

    __slots__ = ("_enabled", "_scopes")

    def __init__(
        self,
        *,
        enabled: Collection[SideEffect] | None = None,
        scopes: Mapping[SideEffect, SideEffectPolicy] | None = None,
    ) -> None:
        self._enabled = frozenset(enabled or ("read",))
        self._scopes = dict(scopes or {})

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> PermissionPolicy:
        """从 ``tool.permission`` 段构造。

        ⚠ **配置路径刻意与 ``tool.enabled`` 分开**（``permission.levels`` / ``permission.scope``）：
        那两个「enabled」是**不同的东西** ——

        - ``tool.enabled``：**工具名**列表（哪些内置工具可用），``build_default_registry`` 读它；
        - ``tool.permission.levels``：**副作用等级**列表（哪些等级被授权），本方法读它。

        它们一度共用了同一个键，于是「工具名里出现 ``grep``」被当成「未知的副作用等级」——
        而**两者各自单独接线时都不会出错**，只有接起来才炸。
        同一个键承载两种语义，迟早会撞。
        """
        data = dict(cfg or {})
        permission = data.get("permission") or {}
        if not isinstance(permission, Mapping):
            raise ValueError(f"tool.permission 必须是映射，得到 {type(permission).__name__}")

        raw_levels = permission.get("levels")
        if raw_levels is None:
            enabled: Collection[SideEffect] = ("read",)
        elif isinstance(raw_levels, (str, bytes)):
            enabled = (str(raw_levels),)  # type: ignore[assignment]
        else:
            enabled = tuple(str(x) for x in raw_levels)  # type: ignore[assignment]

        known = set(_SEVERITY)
        unknown = {str(x) for x in enabled} - known
        if unknown:
            raise ValueError(
                f"tool.permission.levels 里有未知的副作用等级："
                f"{', '.join(sorted(unknown))}；已知：{', '.join(sorted(known))}"
            )

        raw_scopes = permission.get("scope") or {}
        if not isinstance(raw_scopes, Mapping):
            raise ValueError(f"tool.permission.scope 必须是映射，得到 {type(raw_scopes).__name__}")
        scopes = {
            str(level): SideEffectPolicy.from_config(paths)
            for level, paths in raw_scopes.items()
        }

        return cls(enabled=enabled, scopes=scopes)

    # ---------------------------------------------------------------- 判定
    def authorize(self, definition: ToolDefinition) -> PermissionDecision:
        """这个工具有没有被授权。"""
        level = definition.side_effect

        if level not in self._enabled:
            allowed = ", ".join(sorted(self._enabled)) or "（无）"
            return PermissionDecision(
                False,
                f"工具 {definition.name!r} 的副作用等级是 {level!r}，"
                f"未启用（已启用：{allowed}）",
            )

        scope = self._scopes.get(level)
        if scope is None or not scope.granted:
            return PermissionDecision(
                False,
                f"副作用等级 {level!r} 没有配置可访问范围。\n"
                "范围是**默认拒绝**的：没配就是不许访问，不是「默认允许」。",
            )

        return PermissionDecision(True)

    def context_for(
        self,
        definition: ToolDefinition,
        *,
        scope: str = "",
        trace_id: str = "",
        session_id: str | None = None,
        caller: str | None = None,
        limits: Mapping[str, Any] | None = None,
    ) -> ToolContext:
        """造出这次执行的上下文（含**已解析**的可访问范围）。"""
        data = dict(limits or {})
        policy = self._scopes.get(definition.side_effect)
        return ToolContext(
            scope=scope,
            allowed_paths=policy.paths if policy else (),
            trace_id=trace_id,
            session_id=session_id,
            caller=caller,
            max_output_bytes=int(data.get("max_output_bytes", 262_144)),
            max_output_lines=int(data.get("max_output_lines", 2_000)),
            timeout_s=float(data.get("timeout_s", 60.0)),
        )

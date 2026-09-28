"""``write`` —— 写文件（有副作用）。

**为什么是 ``write`` 而不是 ``destructive``**：覆盖同一个路径、写同样的内容，
重复执行的结果与执行一次相同。所以它对**幂等要求**是有的（重复执行会多一次
无意义的写、且可能覆盖掉别人在这期间改的内容），但后果可接受。

**``append`` 模式是例外，必须小心**：追加**天然不可重放** ——
跑两遍就是两份内容。所以追加模式要求调用方给出幂等键（见 ``executor`` 的说明）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from tool.base import Tool, ToolContext, require_text
from tool.types import ToolResult

__all__ = ["WriteTool"]

VALID_MODES = frozenset({"overwrite", "append"})


class WriteTool(Tool):
    """写一个文件（覆盖或追加）。"""

    name: ClassVar[str] = "write"
    description: ClassVar[str] = (
        "写入文件。mode=overwrite 覆盖，mode=append 追加（默认 overwrite）。"
        "目标必须在可写范围内。"
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标文件路径"},
            "content": {"type": "string", "description": "要写入的内容"},
            "mode": {"type": "string", "enum": ["overwrite", "append"]},
            "create_dirs": {"type": "boolean", "description": "是否自动创建父目录"},
        },
        "required": ["path", "content"],
    }
    side_effect: ClassVar[str] = "write"

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        raw = require_text(args, "path", tool=self.name)
        content = str(args.get("content") or "")
        mode = str(args.get("mode") or "overwrite").lower()

        if mode not in VALID_MODES:
            known = ", ".join(sorted(VALID_MODES))
            return ToolResult(
                outcome="refused",
                tool_name=self.name,
                error=f"未知的 mode={mode!r}；已知：{known}",
            )

        target = (ctx.allowed_paths[0] / raw if ctx.allowed_paths else Path(raw))
        # 目标文件可能还不存在，所以 resolve 它自己会失败 —— 解析父目录。
        # **父目录必须解析**：否则 `可写目录/link -> /etc/` 这样的路径能逃出范围。
        try:
            target = target.parent.resolve() / target.name
        except OSError as exc:
            return ToolResult(
                outcome="failed", tool_name=self.name, error=f"路径无法解析：{exc}"
            )

        if not ctx.within(target):
            allowed = ", ".join(str(p) for p in ctx.allowed_paths) or "（未配置任何可写范围）"
            return ToolResult(
                outcome="refused",
                tool_name=self.name,
                error=(
                    f"路径超出可写范围：{target}\n可写范围：{allowed}\n"
                    "（范围默认拒绝：没配就是不许访问）"
                ),
            )

        if args.get("create_dirs"):
            target.parent.mkdir(parents=True, exist_ok=True)

        try:
            if mode == "append":
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(content)
            else:
                target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                outcome="failed",
                tool_name=self.name,
                error=f"写入失败：{type(exc).__name__}: {exc}",
            )

        written = len(content.encode("utf-8"))
        return ToolResult(
            outcome="executed",
            tool_name=self.name,
            output=f"已{mode} {target}（{written} 字节）",
        )

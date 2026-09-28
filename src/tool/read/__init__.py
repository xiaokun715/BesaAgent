"""``read`` —— 读文件（只读）。

**安全上最关键的一步是解析顺序**：先把路径 ``resolve()``（会展开 ``..`` 与符号链接），
**再**做范围判定。反过来写的话，一个指向范围外的符号链接会通过检查 ——
因为检查时看到的还是那个「看起来在范围内」的路径。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from tool.base import Tool, ToolContext, require_text, truncate
from tool.types import ToolResult

__all__ = ["ReadTool"]


class ReadTool(Tool):
    """读一个文件的全部或指定行范围。"""

    name: ClassVar[str] = "read"
    description: ClassVar[str] = (
        "读取文件内容。可选按行范围读（1 起、闭区间）。"
        "输出超过上限会被截断，且会明确标注。"
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "start_line": {"type": "integer", "description": "起始行（1 起，可省略）"},
            "end_line": {"type": "integer", "description": "结束行（闭区间，可省略）"},
        },
        "required": ["path"],
    }
    side_effect: ClassVar[str] = "read"

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        raw = require_text(args, "path", tool=self.name)
        target = (ctx.allowed_paths[0] / raw if ctx.allowed_paths else Path(raw)).resolve()

        # **先 resolve 再判定** —— 见模块 docstring
        if not ctx.within(target):
            allowed = ", ".join(str(p) for p in ctx.allowed_paths) or "（未配置任何可读范围）"
            return ToolResult(
                outcome="refused",
                tool_name=self.name,
                error=(
                    f"路径超出可读范围：{target}\n可读范围：{allowed}\n"
                    "（范围默认拒绝：没配就是不许访问）"
                ),
            )

        if not target.is_file():
            return ToolResult(
                outcome="refused", tool_name=self.name, error=f"不是一个文件：{target}"
            )

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # 读失败是**执行失败**而不是拒绝 —— 文件在，只是这次没读成
            return ToolResult(
                outcome="failed", tool_name=self.name, error=f"读取失败：{type(exc).__name__}: {exc}"
            )

        lines = text.splitlines(keepends=True)
        start = int(args.get("start_line") or 1)
        end = int(args.get("end_line") or 0)
        if start > 1 or end:
            lines = lines[max(0, start - 1) : (end or len(lines))]
            text = "".join(lines)

        output, truncated = truncate(
            text, max_bytes=ctx.max_output_bytes, max_lines=ctx.max_output_lines
        )
        return ToolResult(
            outcome="executed",
            tool_name=self.name,
            output=output,
            truncated=truncated,
        )

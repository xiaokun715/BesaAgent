"""``grep`` —— 按正则搜索（只读）。

**两条边界是刻意的**：

1. **单次返回条数上限**：一次搜索能把整个仓库命中，结果塞进上下文会把
   后续推理挤爆 —— 而模型不知道「你只给了它前几条」。
2. **跳过明显的二进制与大目录**：不跳的话，一次 ``grep`` 会把 `.git` 里的
   压缩包也读一遍，慢且没有意义。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, ClassVar

from tool.base import Tool, ToolContext, require_text, truncate
from tool.types import ToolResult

__all__ = ["GrepTool"]

#: 这些目录里的内容对「找代码」没有价值，但会让一次搜索慢上几个数量级
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}
)

#: 单文件超过这个大小就不搜了 —— 命中的行几乎不会是我们要找的，
#: 而读取它的开销是实打实的（且会被 2GB 的单文件卡住）
_MAX_FILE_BYTES = 2 * 1024 * 1024

DEFAULT_MAX_MATCHES = 200


class GrepTool(Tool):
    """用正则搜索文件内容，返回「文件:行号: 内容」。"""

    name: ClassVar[str] = "grep"
    description: ClassVar[str] = (
        "按正则搜索文件内容。返回 文件:行号: 内容。"
        "结果有上限，被截断时会明确标注。"
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "搜索根（省略则用第一个可读范围）"},
            "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
            "max_matches": {"type": "integer", "description": "命中上限"},
        },
        "required": ["pattern"],
    }
    side_effect: ClassVar[str] = "read"

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        pattern_text = require_text(args, "pattern", tool=self.name)
        try:
            pattern = re.compile(pattern_text)
        except re.error as exc:
            # 正则写错是**参数问题**，该回给模型让它改，不该当作执行失败
            return ToolResult(
                outcome="refused", tool_name=self.name, error=f"正则非法：{exc}"
            )

        raw_root = args.get("path")
        root = (
            Path(str(raw_root)) if raw_root else (ctx.allowed_paths[0] if ctx.allowed_paths else Path("."))
        )
        root = root.resolve()
        if not ctx.within(root):
            allowed = ", ".join(str(p) for p in ctx.allowed_paths) or "（未配置任何可读范围）"
            return ToolResult(
                outcome="refused",
                tool_name=self.name,
                error=f"搜索根超出可读范围：{root}\n可读范围：{allowed}",
            )

        glob = str(args["glob"]) if args.get("glob") else None
        limit = int(args.get("max_matches") or DEFAULT_MAX_MATCHES)

        matches: list[str] = []
        truncated = False
        for file_path in self._walk(root, glob):
            if len(matches) >= limit:
                truncated = True
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    matches.append(f"{file_path}:{number}: {line.rstrip()}")
                    if len(matches) >= limit:
                        truncated = True
                        break

        body, cut_by_size = truncate(
            "\n".join(matches), max_bytes=ctx.max_output_bytes, max_lines=ctx.max_output_lines
        )
        return ToolResult(
            outcome="executed",
            tool_name=self.name,
            output=body,
            # 两个来源都要报：条数到了上限、或字节/行数到了上限
            truncated=truncated or cut_by_size,
        )

    @staticmethod
    def _walk(root: Path, glob: str | None) -> Iterator[Path]:
        if root.is_file():
            yield root
            return
        for candidate in root.rglob(glob or "*"):
            if not candidate.is_file():
                continue
            if any(part in _SKIP_DIRS for part in candidate.parts):
                continue
            try:
                if candidate.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield candidate

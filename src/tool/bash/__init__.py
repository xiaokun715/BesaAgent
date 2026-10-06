"""``bash`` —— 执行 shell 命令（``destructive``，默认关闭）。

⚠ **拒绝清单不是安全边界。**

它挡的是「手滑」而不是「恶意」—— 一个足够长的命令、一个脚本文件、
一次 ``python -c``，都能绕过任何字符串匹配。真正的边界是**三件事的组合**：

1. 这个工具**默认关闭**（``DT-6``），要显式打开；
2. 它的``side_effect`` 是 ``destructive``，于是**必须显式授权**才能用；
3. 它有超时与输出上限，且**执行记录进库**（谁在什么时候跑了什么，可审计）。

把「配了拒绝清单」当成安全措施，比不配更危险 —— 它会让人放松前三条。
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any, ClassVar

from tool.base import Tool, ToolContext, require_text
from tool.sandbox import Sandbox
from tool.types import ToolResult

__all__ = ["DENYLIST", "BashTool"]

#: 最低限度的兜底。**不是安全边界** —— 见模块 docstring。
#: 只挡那些「一眼就该停下来」的形态：抹掉根目录、格式化文件系统、fork 炸弹。
DENYLIST: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero",
    ":(){ :|:& };:",
    "> /dev/sda",
)


class BashTool(Tool):
    """执行一条 shell 命令。"""

    name: ClassVar[str] = "bash"
    description: ClassVar[str] = (
        "执行 shell 命令并返回输出。有超时与输出上限。"
        "**这个工具能改变系统状态，默认不启用。**"
    )
    parameters: ClassVar[Mapping[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"},
            "cwd": {"type": "string", "description": "工作目录（必须在可写范围内）"},
            "timeout_s": {"type": "number", "description": "超时秒数"},
        },
        "required": ["command"],
    }
    side_effect: ClassVar[str] = "destructive"

    def __init__(self, sandbox: Sandbox | None = None) -> None:
        # **沙箱由外部注入**：资源边界与清理过的环境是**跨工具**的关注点，
        # 让每个工具自己 new 一个的结果是「有一个用了默认值而没人发现」。
        self._sandbox = sandbox or Sandbox()

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        command = require_text(args, "command", tool=self.name)

        hit = next((bad for bad in DENYLIST if bad in command), None)
        if hit:
            return ToolResult(
                outcome="refused",
                tool_name=self.name,
                error=(
                    f"命令命中拒绝清单（{hit!r}）。\n"
                    "注意：拒绝清单**不是安全边界**，它只挡手滑；"
                    "真正的边界是这个工具默认关闭 + 需要显式授权。"
                ),
            )

        cwd = None
        if args.get("cwd"):
            candidate = (ctx.allowed_paths[0] / str(args["cwd"])).resolve()
            if not ctx.within(candidate):
                return ToolResult(
                    outcome="refused",
                    tool_name=self.name,
                    error=f"工作目录超出授权范围：{candidate}",
                )
            cwd = candidate

        # 子进程的**全部边界**交给沙箱：超时真 kill、输出上限、
        # 以及**不继承宿主环境**（继承的话 `bash env` 能把宿主的密钥全打出来）。
        outcome = await self._sandbox.run_process(
            command,
            ctx=ctx,
            cwd=cwd,
            timeout_s=float(args.get("timeout_s") or ctx.timeout_s),
        )

        if outcome.timed_out:
            return ToolResult(
                outcome="failed",
                tool_name=self.name,
                error=(
                    f"命令超时（{ctx.timeout_s}s），已终止。"
                    "**注意：超时前它可能已经产生了副作用。**"
                ),
                elapsed_s=outcome.duration_s,
            )

        if outcome.returncode != 0:
            # 非零退出是**执行失败**而不是拒绝：命令跑了，只是它失败了。
            # 这一区分很重要 —— 上层据此判断「要不要重试」。
            return ToolResult(
                outcome="failed",
                tool_name=self.name,
                output=outcome.output,
                error=f"命令以退出码 {outcome.returncode} 结束",
                truncated=outcome.truncated,
                elapsed_s=outcome.duration_s,
            )

        return ToolResult(
            outcome="executed",
            tool_name=self.name,
            output=outcome.output,
            truncated=outcome.truncated,
            elapsed_s=outcome.duration_s,
        )


def quote(command: str) -> str:
    """把一段文本安全地嵌进 shell 命令（供上层拼命令时用）。"""
    return shlex.quote(command)

"""沙箱：让工具执行发生在一个**有界**的环境里（`FR-T-15`）。

**为什么这些限制不能由每个工具各写一遍**：结果是「有一个漏了」。
收在一处之后，工具只管「执行」，边界由包装层负责 ——
与 ``permission.py`` 收走路径判定是同一个手法。

## ⚠ 一期是**软限制**，这一点必须能被读到

跨平台的硬隔离（cgroup / Job Object / netns）成本高，
一期**只做能确定做对的**那几项：

============================  ==============  ==========================
限制                           一期（soft）     靠什么
============================  ==============  ==========================
墙钟时间                       ✅ 真的 kill     ``asyncio.wait_for`` + ``kill()``
输出（字节 / 行）               ✅              ``truncate``
环境变量                       ✅              ``clean_env``（不继承宿主）
工作目录                       ✅              独立子目录
可写范围                       ✅              ``permission.py`` 的范围判定
CPU 时间                       ❌ 未强制       需要 cgroup / rlimit
内存                           ❌ 未强制       同上
进程数                         ❌ 未强制       同上
网络                           ❌ 未强制       需要 netns / firewall
============================  ==============  ==========================

**把「❌」写出来比硬上一半更重要**：让人以为「配了就安全」比不配更危险。
所以 :meth:`Sandbox.describe` 会把这些如实说出来，而配置里若设了未强制的那几项，
**启动期会告警** —— 否则那份配置就是一句谎话（设了，但不生效，且没人知道）。

这与 ``bash`` 的拒绝清单是同一条纪律：**它不是安全边界，别当成安全边界用。**
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tool.base import ToolContext, truncate

__all__ = ["ENFORCED_IN_SOFT", "LIMITS_NEEDING_ISOLATION", "ProcessOutcome", "Sandbox", "SandboxLimits"]

_log = logging.getLogger(__name__)

SandboxMode = Literal["soft", "isolated"]

#: 软限制下**真的**被强制的项。
ENFORCED_IN_SOFT: frozenset[str] = frozenset(
    {"timeout_s", "max_output_bytes", "max_output_lines", "clean_env", "isolated_workdir"}
)

#: 需要硬隔离才能强制的项。配置里设了它们而 mode 是 soft → 启动期告警。
LIMITS_NEEDING_ISOLATION: frozenset[str] = frozenset(
    {"max_cpu_s", "max_memory_mb", "max_processes", "allow_network"}
)

#: 干净环境里**保留**的变量。
#:
#: 这个白名单是刻意短的。**不继承宿主环境**是这条里最重要的一件：
#: 继承的话 ``bash env`` 能把宿主上的密钥全打出来 —— 而它只需要一个 ``env`` 参数就能避免。
#: 保留的这几项是为了让命令**跑得起来**（Windows 上缺 ``SystemRoot`` 连 cmd 都起不来）。
_KEEP_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMROOT",
    "WINDIR",
    "USERPROFILE",
    "NUMBER_OF_PROCESSORS",
)


@dataclass(frozen=True)
class SandboxLimits:
    """沙箱配置。默认值来自 ``tool.limits`` / ``tool.sandbox``。"""

    mode: SandboxMode = "soft"
    timeout_s: float = 60.0
    max_output_bytes: int = 262_144
    max_output_lines: int = 2_000

    # ---- 下面几项**软限制下不生效**，见模块 docstring ----
    max_cpu_s: float = 30.0
    max_memory_mb: int = 512
    max_processes: int = 32

    clean_env: bool = True
    isolated_workdir: bool = True
    allow_network: bool = False

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> SandboxLimits:
        data = dict(cfg or {})
        limits = dict(data.get("limits") or {})
        sandbox = dict(data.get("sandbox") or {})

        mode = str(sandbox.get("mode", "soft")).strip().lower()
        if mode not in ("soft", "isolated"):
            raise ValueError(f"未知的 sandbox.mode={mode!r}；已知：soft, isolated")

        return cls(
            mode=mode,  # type: ignore[arg-type]
            timeout_s=float(limits.get("timeout_s", 60)),
            max_output_bytes=int(limits.get("max_output_bytes", 262_144)),
            max_output_lines=int(limits.get("max_output_lines", 2_000)),
            max_cpu_s=float(limits.get("max_cpu_s", 30)),
            max_memory_mb=int(limits.get("max_memory_mb", 512)),
            max_processes=int(limits.get("max_processes", 32)),
            clean_env=bool(sandbox.get("clean_env", True)),
            isolated_workdir=bool(sandbox.get("isolated_workdir", True)),
            allow_network=bool(sandbox.get("allow_network", False)),
        )

    @property
    def is_soft(self) -> bool:
        return self.mode == "soft"

    def unenforced(self) -> tuple[str, ...]:
        """当前模式下**没有真的生效**的限制项。

        非空时启动期会告警 —— 一份「设了但不生效」的配置比没设更危险，
        因为它会让人以为那件事已经被管住了。
        """
        if not self.is_soft:
            return ()
        return tuple(sorted(LIMITS_NEEDING_ISOLATION))


@dataclass(frozen=True)
class ProcessOutcome:
    """一次子进程执行的结果。"""

    returncode: int = 0
    output: str = ""
    truncated: bool = False
    timed_out: bool = False
    #: 是否被**真的杀掉**。超时只放弃等待而不 kill 的话，
    #: 子进程的副作用还在继续，而调用方以为「这次超时了、什么都没做」
    killed: bool = False
    duration_s: float = 0.0


@dataclass
class Sandbox:
    """执行包装层。目前只有子进程这一类，未来隔离模式也走同一个入口。"""

    limits: SandboxLimits = field(default_factory=SandboxLimits)

    def __post_init__(self) -> None:
        unenforced = self.limits.unenforced()
        if unenforced:
            # **只告警一次**（构造期）而不是每次调用都刷屏。
            _log.warning(
                "沙箱运行在 soft 模式下，以下限制**没有真正生效**：%s。\n"
                "它们需要硬隔离（容器 / cgroup / Job Object）。"
                "**不要把 soft 模式当成安全边界** —— 它挡手滑，挡不住刻意绕过。",
                ", ".join(unenforced),
            )

    # ---------------------------------------------------------------- 描述
    @property
    def mode(self) -> SandboxMode:
        return self.limits.mode

    def describe(self) -> str:
        """「当前挡得住什么、挡不住什么」—— 供 ``doctor`` 与启动日志用。

        `NFR-T-10` 要求这条**在配置、启动日志、doctor 三处都能看到**：
        让人以为「配了就安全」，比不配更危险。
        """
        enforced = "、".join(sorted(ENFORCED_IN_SOFT & set(self.limits.__dataclass_fields__)))
        unenforced = self.limits.unenforced()
        line = f"沙箱模式：{self.limits.mode}（已强制：{enforced}）"
        if unenforced:
            line += (
                f"\n  ⚠ 未强制：{', '.join(unenforced)}"
                f" —— soft 模式**不是安全边界**"
            )
        return line

    # ---------------------------------------------------------------- 环境
    def environment(self, ctx: ToolContext) -> dict[str, str]:
        """构造子进程的环境变量。

        ``clean_env`` 为真时**只保留白名单**（见 :data:`_KEEP_ENV`）——
        宿主上的 ``*_API_KEY`` 之类一个都不带过去。
        """
        if not self.limits.clean_env:
            return dict(os.environ)

        env = {name: os.environ[name] for name in _KEEP_ENV if name in os.environ}

        # 临时目录指到工作目录里，别让它写宿主的 /tmp
        if ctx.allowed_paths:
            scratch = ctx.allowed_paths[0] / ".tool_tmp"
            try:
                scratch.mkdir(parents=True, exist_ok=True)
            except OSError:  # pragma: no cover - 权限问题，交给执行去报
                scratch = ctx.allowed_paths[0]
            env["TMPDIR"] = str(scratch)
            env["TEMP"] = str(scratch)
            env["TMP"] = str(scratch)
        return env

    # ---------------------------------------------------------------- 执行
    async def run_process(
        self,
        command: str,
        *,
        ctx: ToolContext,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> ProcessOutcome:
        """跑一条 shell 命令，套上这个沙箱能给的边界。

        **超时必须真的杀掉**：只放弃等待的话，子进程会继续跑 ——
        它的副作用还在发生，而调用方以为「这次超时了、什么都没做」。
        """
        effective_timeout = float(timeout_s or ctx.timeout_s or self.limits.timeout_s)
        started = time.monotonic()

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd) if cwd else None,
                env=dict(env) if env is not None else self.environment(ctx),
            )
        except OSError as exc:
            return ProcessOutcome(returncode=-1, output=f"无法启动命令：{exc}", duration_s=0.0)

        timed_out = False
        killed = False
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=effective_timeout)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            killed = True
            await process.wait()
            stdout = b""

        text = stdout.decode("utf-8", errors="replace")
        body, truncated = truncate(
            text,
            max_bytes=ctx.max_output_bytes or self.limits.max_output_bytes,
            max_lines=ctx.max_output_lines or self.limits.max_output_lines,
        )
        return ProcessOutcome(
            returncode=process.returncode or 0,
            output=body,
            truncated=truncated,
            timed_out=timed_out,
            killed=killed,
            duration_s=time.monotonic() - started,
        )

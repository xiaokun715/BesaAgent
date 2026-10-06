"""结果处置：脱敏、截断、落盘（`FR-T-18` / `FR-T-19`）。

**为什么结果需要一整套处置**：工具的输出会被**直接回灌给模型**，
于是它同时是三个东西 —— 可能带密钥的数据、可能被注入的内容、可能大得放不下的文本。
三件事各自有对应的处理，而它们必须在**同一个地方**做掉：
散给每个工具自己做，结果就是「有一个漏了」。

**三档处置**（`FR-T-18`）：

    ==========  ==============================================
    小          原样返回
    中          截断 + **明确标注**
    大          **落盘 + 返回路径**，让模型按需再读
    ==========  ==============================================

**为什么非要有第三档**：截断是**不可逆的信息丢失**。
模型拿到一个被截断的开头，然后**基于它下结论** —— 而它不知道后面还有什么。
落盘 + 路径让它能自己决定要不要看、看哪一段。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from foundation.errors import redact_secrets
from tool.base import ToolContext, truncate
from tool.types import ToolResult

__all__ = ["ResultPolicy", "ResultProcessor", "SPILL_DIR_NAME"]

_log = logging.getLogger(__name__)

#: 落盘子目录名。**必须在模型的可见范围内**（`FR-T-18`）——
#: 否则它拿到路径也读不了，那一档处置就白做了。
SPILL_DIR_NAME = ".tool_out"


@dataclass(frozen=True)
class ResultPolicy:
    """结果处置的配置。"""

    #: 回灌给模型前抹掉疑似密钥
    redact: bool = True
    #: 超过它就落盘（字节）。**不是「截断阈值」** —— 中档截断由 ``ToolContext`` 管
    spill_threshold_bytes: int = 65_536
    #: 落盘目录。``None`` 表示用第一个可写范围下的 ``.tool_out/``
    spill_dir: Path | None = None
    #: 落盘总量上限（MB）。**没有「不限」这个选项**（`C-T-9`）：
    #: 跑飞的 agent 靠「每次输出都很大」就能把磁盘写满，而这个上限是唯一的刹车
    spill_total_quota_mb: int = 512
    #: 落盘文件保留多久（小时）。**惰性清理** —— 每次写入时顺带扫一遍，
    #: 不需要调度器，也不需要处理「清理任务自己崩了」
    spill_ttl_hours: int = 24
    #: 摘要里给模型看多少行
    summary_lines: int = 40

    def resolve_dir(self, ctx: ToolContext) -> Path | None:
        """算出这次该往哪落盘。范围为空时**不落盘**（退回截断）。"""
        if self.spill_dir is not None:
            return Path(self.spill_dir)
        if ctx.allowed_paths:
            return ctx.allowed_paths[0] / SPILL_DIR_NAME
        return None


@dataclass
class ResultProcessor:
    """把工具的原始输出加工成「可以给模型看的东西」。

    **无状态的设计**（除了配额扫描的缓存）—— 每次处理需要的都在参数里，
    所以一个实例可以被并发使用。
    """

    policy: ResultPolicy = field(default_factory=ResultPolicy)

    # ---------------------------------------------------------------- 入口
    async def process(
        self,
        result: ToolResult,
        *,
        ctx: ToolContext,
        idem_key: str = "",
    ) -> ToolResult:
        """按顺序做四件事：脱敏 → 定档 → （截断 | 落盘）。"""
        if not result.ok or not result.output:
            return result

        text = result.output
        redacted = False

        # ① 脱敏。**不可逆** —— 原始输出有意不保留，这是设计的一部分：
        #    留一份「脱敏前的」等于把密钥又攒了一处。
        if self.policy.redact:
            cleaned = redact_secrets(text)
            redacted = cleaned != text
            if redacted:
                _log.info(
                    "工具 %s 的输出里检出疑似密钥，已脱敏（%d → %d 字符）",
                    result.tool_name,
                    len(text),
                    len(cleaned),
                )
            text = cleaned

        # ② 定档：大 → 落盘
        raw = text.encode("utf-8")
        if len(raw) > self.policy.spill_threshold_bytes:
            spilled = await self._spill(text, ctx=ctx, idem_key=idem_key, source=result)
            if spilled is not None:
                return replace(
                    result,
                    output=self._summary(text),
                    spilled_path=str(spilled),
                    redacted=redacted,
                    truncated=False,
                )
            # 落不下（没范围 / 超配额）→ **退回截断**，并且要让调用方知道
            # 「本该落盘但没能落」。静默退回会让模型以为这就是全部。
            text = (
                f"[注意：本次输出 {len(raw)} 字节，**本该落盘但没能落**"
                f"（没有可写范围或超出配额），下面是截断后的开头]\n{text}"
            )

        # ③ 截断（连同标注）
        body, cut = truncate(
            text, max_bytes=ctx.max_output_bytes, max_lines=ctx.max_output_lines
        )
        if cut:
            body += (
                f"\n…（输出被截断：上限 {ctx.max_output_bytes} 字节 / "
                f"{ctx.max_output_lines} 行）"
            )
        return replace(result, output=body, redacted=redacted, truncated=cut)

    # ---------------------------------------------------------------- 落盘
    async def _spill(
        self,
        text: str,
        *,
        ctx: ToolContext,
        idem_key: str,
        source: ToolResult,
    ) -> Path | None:
        directory = self.policy.resolve_dir(ctx)
        if directory is None:
            _log.warning(
                "工具 %s 的输出很大（%d 字节），但没有可写范围可落盘 —— 退回截断。"
                "**模型会拿到一个残缺的开头而不知道后面还有什么。**",
                source.tool_name,
                len(text.encode("utf-8")),
            )
            return None

        return await asyncio.to_thread(
            self._spill_sync, directory, text, idem_key, source.tool_name
        )

    def _spill_sync(
        self, directory: Path, text: str, idem_key: str, tool_name: str
    ) -> Path | None:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("落盘目录建不出来（%s）：%s", directory, exc)
            return None

        # **惰性清理**：每次写入顺带扫一遍过期的。比 TTL 调度器少一整套
        # 「调度器自己崩了怎么办」的问题。
        self._sweep(directory)

        payload = text.encode("utf-8")
        quota = self.policy.spill_total_quota_mb * 1024 * 1024
        if self._total_bytes(directory) + len(payload) > quota:
            _log.warning(
                "落盘总量已达上限（%d MB），本次不落盘 —— 退回截断。"
                "调大 tool.result.spill_total_quota_mb，或检查是不是有 agent 在跑飞。",
                self.policy.spill_total_quota_mb,
            )
            return None

        # 文件名带上幂等键与纳秒时间：**排障时能从路径反查到是哪次调用**
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        suffix = (idem_key or "nokey").replace("/", "_").replace(":", "_")[-24:]
        target = directory / f"{tool_name}-{stamp}-{suffix}.txt"
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            _log.warning("落盘失败（%s）：%s", target, exc)
            return None
        return target

    def _sweep(self, directory: Path) -> None:
        cutoff = time.time() - self.policy.spill_ttl_hours * 3600
        for item in directory.glob("*.txt"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                continue

    @staticmethod
    def _total_bytes(directory: Path) -> int:
        total = 0
        for item in directory.glob("*.txt"):
            try:
                total += item.stat().st_size
            except OSError:
                continue
        return total

    def _summary(self, text: str) -> str:
        """落盘后给模型的摘要：**头几行 + 总量**。

        必须带上「总共有多少」—— 只说「前面是这样的」，
        模型不知道自己没看全，于是会基于摘要下结论 ——
        那与「截断而不标注」是同一个错误。
        """
        lines = text.splitlines()
        head = "\n".join(lines[: self.policy.summary_lines])
        return (
            f"（以下是前 {min(len(lines), self.policy.summary_lines)} 行，"
            f"全文共 {len(lines)} 行 / {len(text.encode('utf-8'))} 字节）\n{head}"
        )

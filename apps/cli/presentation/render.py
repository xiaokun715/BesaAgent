"""把结果渲染成人看的文本。

**这一层只做格式化，不做决策** —— 它不判断「降级了要不要警告」，
只把 ``GatewayResult`` 里已经带好的事实显示出来。
一旦这里开始加判断，就会出现「CLI 会提示、server 不会」这类不一致。
"""

from __future__ import annotations

from typing import TextIO

from gateway.types import AttemptRecord, GatewayResult, StreamChunk, StreamDone, StreamFailed

__all__ = ["render_attempts", "render_result", "render_stream_event"]


def render_result(result: GatewayResult, *, out: TextIO) -> None:
    """打印一次调用的结果与它的「账单」。"""
    print(result.content, file=out)
    print(file=out)
    print(_summary(result), file=out)

    if result.degraded:
        # 降级必须显式提示 —— 静默降级会让「为什么这次答得差 / 花得多」
        # 变成一个用户永远想不明白的问题（FR-G-05）。
        print("\n⚠️  本次发生了降级（首个候选不可用或失败）", file=out)
        print(render_attempts(result.attempts), file=out)


def render_stream_event(event: object, *, out: TextIO) -> None:
    """流式事件的增量渲染。"""
    if isinstance(event, StreamChunk):
        print(event.text, end="", flush=True, file=out)
    elif isinstance(event, StreamDone):
        print("\n", file=out)
        print(_summary(event.result), file=out)
    elif isinstance(event, StreamFailed):
        print("\n", file=out)
        print(f"✗ 流式失败：{event.error}", file=out)
        if event.attempts:
            print(render_attempts(event.attempts), file=out)


def render_attempts(attempts: tuple[AttemptRecord, ...]) -> str:
    """把尝试链渲染成缩进列表。"""
    lines = ["  尝试记录："]
    for index, record in enumerate(attempts, start=1):
        if record.skipped:
            lines.append(f"    {index}. {record.model_key} — 跳过（{record.skipped_reason}）")
        elif record.outcome == "success":
            lines.append(f"    {index}. {record.model_key} — 成功（{record.elapsed_s:.3f}s）")
        else:
            retryable = "可重试" if record.retryable else "不可重试"
            lines.append(
                f"    {index}. {record.model_key} — 失败（{retryable}）：{record.error}"
            )
    return "\n".join(lines)


def _summary(result: GatewayResult) -> str:
    return (
        f"模型 {result.model_key}｜逻辑名 {result.alias}｜"
        f"token {result.usage.input_tokens}↓/{result.usage.output_tokens}↑｜成本 {result.cost}"
        + (f"｜trace {result.trace_id}" if result.trace_id else "")
    )

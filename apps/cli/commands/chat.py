"""``besa runtime`` —— 发一条消息。

**这个文件是「provider/gateway 契约够不够用」的试金石**：
它只写业务意图（问一句话、要个回答），不出现任何厂商名、模型名、重试逻辑。
如果为了让它跑起来需要在这里加判断，那说明下层漏了东西。
"""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from apps.cli.presentation.render import render_result, render_stream_event
from foundation.logging import current_trace_id
from gateway.errors import GatewayError
from provider.types import Message

__all__ = ["add_parser", "run"]

ALIAS = "runtime.default"


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("runtime", help="向默认模型发一条消息")
    parser.add_argument("message", help="用户消息")
    parser.add_argument("--session", default=None, help="会话标识（用于用量归集）")
    parser.add_argument("--stream", action="store_true", help="流式输出")
    parser.add_argument("--deadline", type=float, default=None, help="总超时预算（秒）")
    parser.set_defaults(handler=run)


async def run(runtime, args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    stream_out = out or sys.stdout
    messages = [Message.text("user", args.message)]
    # trace_id 由 runtime 在启动时绑定到日志上下文；这里取出来透传给 gateway，
    # 让日志行、事件与用量记录共用同一个 —— 否则「一次失败横跨 3 个模型」无法归因。
    trace_id = current_trace_id()

    try:
        if args.stream:
            async for event in runtime.gateway.stream_chat(
                ALIAS,
                messages,
                session_id=args.session,
                deadline_s=args.deadline,
                trace_id=trace_id,
            ):
                render_stream_event(event, out=stream_out)
        else:
            result = await runtime.gateway.chat(
                ALIAS,
                messages,
                session_id=args.session,
                deadline_s=args.deadline,
                trace_id=trace_id,
            )
            render_result(result, out=stream_out)
    except GatewayError as exc:
        # gateway 的错误已经归一化过（带尝试记录），直接展示即可 ——
        # 上层不需要知道底层是哪个厂商、什么状态码。
        print(f"✗ {exc}", file=stream_out)
        return 1

    return 0

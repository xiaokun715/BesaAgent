"""``besa run`` —— 跑一个**任务**（走单 agent 运行时）。

与 ``besa runtime`` 的分工是刻意的（`MA-15`）：

============================  ==================================================
``besa runtime "问一句"``        **直接**发一条消息给模型。它是"provider / gateway
                                契约够不够用"的**试金石** —— 那里不该出现任何
                                厂商名、重试逻辑或工具编排。**保留它。**
``besa run "做个任务"``          **走 agent**：路由到阶段、带工具、有预算上界、
                                可续跑。真正干活的是这一条。
============================  ==================================================

把试金石换成 agent 会**掩盖下层的问题** —— 以后没人能一眼看出
"provider 契约是不是还够用"。
"""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from agent.stages import UnknownStageError

__all__ = ["add_parser", "run"]


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("run", help="跑一个任务（走单 agent 运行时）")
    parser.add_argument("task", help="任务描述")
    parser.add_argument("--stage", default=None,
                        help="显式指定阶段（requirement/test_design/test_case/…）；不给则自动路由")
    parser.add_argument("--session", default=None, help="会话标识（多轮任务用同一个）")
    parser.add_argument("--caller", default="cli", help="调用方标识")
    parser.add_argument("--resume", default=None, metavar="RUN_ID",
                        help="续跑指定的 run_id")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    parser.set_defaults(handler=run)


async def run(runtime, args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    stdout = out or sys.stdout

    if runtime.agent is None:
        print(
            "✗ 单 agent 运行时未启用（agent.enabled=false）——\n"
            "  这条命令需要它。要直接问模型一句话，请用 `besa runtime`。",
            file=stdout,
        )
        return 2

    try:
        outcome = await runtime.agent.run_one(
            args.task,
            stage=args.stage,
            session_id=args.session,
            caller=args.caller,
            resume=bool(args.resume),
            run_id=args.resume or "",
        )
    except UnknownStageError as exc:
        # 阶段名拼错是**最高频**的配置错误 —— 错误信息里已经带了全部可用阶段
        print(f"✗ {exc}", file=stdout)
        return 2
    except ValueError as exc:
        # 比如 on_unknown=reject 时路由不出来
        print(f"✗ {exc}", file=stdout)
        return 2

    if args.json:
        import json
        from dataclasses import asdict

        print(json.dumps(asdict(outcome), ensure_ascii=False, default=str), file=stdout)
    else:
        _render(outcome, out=stdout)

    # **退出码要对得上状态** —— 脚本靠它判断，而"跑完了但没成功"是最容易被误判的一种。
    return {"done": 0, "needs_review": 3, "cancelled": 4}.get(outcome.status, 1)


def _render(outcome, *, out: TextIO) -> None:
    from agent.runtime import RunOutcome

    assert isinstance(outcome, RunOutcome)
    print(str(outcome), file=out)
    if outcome.routing is not None and outcome.routing.stage:
        print(f"  路由：{outcome.routing.reason}", file=out)
    if outcome.needs_review:
        # **"需要人看一眼"要给出下一步** —— 只说"需要人工介入"等于把问题推回去
        print(f"  需要人工：{outcome.reason}", file=out)
        print(f"  （处理完后可用 `besa run --resume {outcome.run_id} \"…\"` 接着跑）", file=out)
    if outcome.answer:
        print("-" * 60, file=out)
        print(outcome.answer, file=out)

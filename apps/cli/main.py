"""BesaAgent CLI 入口。

::

    PYTHONPATH=src python -m apps.cli.main doctor
    PYTHONPATH=src python -m apps.cli.main chat "帮我为登录接口设计测试用例"
    PYTHONPATH=src python -m apps.cli.main --env dev chat "..." --stream

**入口只做三件事**：解析参数、装配运行时、把活派给子命令。
所有业务逻辑在 ``commands/`` 里，所有渲染在 ``presentation/`` 里 ——
``main.py`` 里出现业务判断就是分层的开始崩塌。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from apps.cli.commands import chat, doctor
from apps.cli.runtime import open_runtime
from foundation.errors import redact_secrets
from foundation.settings import SettingsError

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="besa",
        description="BesaAgent —— 面向软件测试全流程的多智能体平台",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="配置环境（对应 configs/<env>.yaml）；缺省取 BESA_ENV，再缺省 dev",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    chat.add_parser(subparsers)
    doctor.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        runtime = open_runtime(args.env)
    except SettingsError as exc:
        # 配置错误是**用户可见**的失败，不该以回溯的形式抛出 ——
        # 回溯会淹没「哪个文件的哪个字段错了」这条真正有用的信息。
        print(f"配置错误：{redact_secrets(str(exc))}", file=sys.stderr)
        return 2

    async def go() -> int:
        try:
            return await args.handler(runtime, args)
        finally:
            await runtime.aclose()

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

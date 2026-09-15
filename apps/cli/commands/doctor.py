"""``besa doctor`` —— 打印装配结果。

**为什么这个命令值得存在**：装配期最常被问的两个问题是

    「为什么这个模型没被选中？」      → 看 available 与 unavailable_reason
    「这条逻辑名到底会走哪个模型？」  → 看候选链

没有它，这两个问题都得靠翻日志或加 print。有了它，配置问题自解释 ——
这也是 ``Registry`` 用**软失败 + 保留原因**而不是直接丢掉不可用模型的原因：
丢掉就没得看了。
"""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

__all__ = ["add_parser", "run"]


def add_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser("doctor", help="打印装配结果与配置自检信息")
    parser.set_defaults(handler=run)


async def run(runtime, args: argparse.Namespace, *, out: TextIO | None = None) -> int:
    stream = out or sys.stdout
    settings = runtime.settings

    print(f"环境：{settings.env}", file=stream)
    print("配置来源：", file=stream)
    for source in settings.sources:
        print(f"  - {source}", file=stream)

    print("\n逻辑名 → 候选链：", file=stream)
    for name in sorted(runtime.registry.aliases):
        alias = runtime.registry.aliases[name]
        chain = " → ".join(spec.key for spec in runtime.registry.candidates(name))
        print(f"  {name}  [{','.join(alias.strategy)}]", file=stream)
        print(f"      {chain}", file=stream)

    print("\n模型：", file=stream)
    for key in sorted(runtime.registry.models):
        spec = runtime.registry.models[key]
        capabilities = ",".join(sorted(capability.value for capability in spec.capabilities))
        if spec.available:
            print(f"  ✓ {key}  vendor={spec.provider}  caps=[{capabilities}]", file=stream)
        else:
            # 不可用的原因必须打出来：这才是 doctor 的主要价值
            print(f"  ✗ {key}  vendor={spec.provider}", file=stream)
            print(f"      不可用：{spec.unavailable_reason}", file=stream)

    unhealthy = [
        item for item in runtime.gateway.health.snapshot() if item["state"] != "closed"
    ]
    if unhealthy:
        print("\n熔断状态：", file=stream)
        for item in unhealthy:
            print(f"  {item['model']}：{item['state']}", file=stream)

    pending = len(runtime.gateway.ledger.records)
    if pending:
        print(f"\n未交付的用量记录：{pending} 条", file=stream)

    return 0

"""CLI 的**应用级**运行时。

它只做三件事，然后交给共享组合根（``src/composition``）：

1. 读配置、装日志（日志级别来自配置，所以必须先读配置）；
2. **建存储**（默认是内存 SQLite）并注入组合根 —— ``src/`` 不能 import ``apps/``，
   所以「用哪个后端」这件事只能在 app 层决定；
3. 起 trace_id 并绑到当前上下文，再跑一次工具体检。

**app 层与组合根的分工**（``src/composition/__init__.py``）：
组合根负责「配置 → 注册表 → 网关 → 工具」这套**共享装配**；
app 层负责自己特有的东西 —— CLI 是日志、trace_id 与存储后端选择，
server 会是 lifespan 与信号处理，MCP 会是会话管理。

**为什么 ``open_runtime`` 是 async 的**：建 SQLite 的表要 await。
早先它是同步的，于是 CLI 只能「不注入 database」—— 那又会让**有副作用的工具
被拒**（幂等没有权威记录 = 真的没有幂等保护）。建表是一件异步的事，
把入口改成 async 比在别处绕开它更诚实。

**CLI 不接 Redis**：单进程没有跨进程去重的需求。于是幂等走**只用权威记录的慢路径**
（``IdempotencyStore`` 不可用 → 语义完全正确，只是每次多一次库往返）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.cli.storage import sqlite as cli_storage
from composition.bootstrap import Runtime, build_runtime
from foundation.database import Database
from foundation.ids import new_trace_id
from foundation.logging import set_trace_id, setup_logging
from foundation.settings import Settings, load_config

__all__ = ["open_runtime", "runtime_env_vars"]


async def open_runtime(
    env: str | None = None,
    *,
    settings: Settings | None = None,
    trace_id: str | None = None,
    database: Database | None = None,
    **kwargs: Any,
) -> Runtime:
    """装配运行时并初始化 CLI 的进程级状态。

    Args:
        env: 环境名；``None`` 时取 ``BESA_ENV``。
        settings: 已加载的配置（测试用）。
        trace_id: 显式指定（便于复现某次调用）；缺省新生成一个。
        database: 显式注入的存储。``None`` 时建一个**内存 SQLite**（建好表），
            于是工具与用量在进程内可用，退出即丢 —— 这正是 CLI 想要的语义。
        **kwargs: 转交 :func:`composition.bootstrap.build_runtime`
            （测试用它注入 ``provider_options`` / ``idempotency_store``）。

    Returns:
        ``Runtime``。**调用方负责 ``aclose()``** —— 它会先 flush 再关连接。
    """
    cfg = settings or load_config(env)

    # 日志级别在配置里，所以装日志必须晚于读配置
    setup_logging(cfg.get("logging.level"))
    set_trace_id(trace_id or new_trace_id())

    store = database
    if store is None:
        store = await cli_storage.open_database()

    runtime = build_runtime(cfg, database=store, **kwargs)

    # 工具体检（重复 / 冲突）。**失败不阻断启动** —— 它内部捕获并告警；
    # 但必须在启动后不久跑一次：「在有人用这些工具之前发现它们重复了」才是它的价值。
    await runtime.inspect_tools()
    return runtime


def runtime_env_vars(runtime: Runtime) -> Mapping[str, str]:
    """供 ``doctor`` 之类查看「到底读到了哪些环境变量名」（**不含值**）。"""
    return {name: "***" if value else "" for name, value in runtime.settings.env_vars.items()}

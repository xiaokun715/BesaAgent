"""CLI 的**应用级**运行时。

它只做两件事，然后交给共享组合根（``src/composition``）：

1. 读配置、装日志（日志级别来自配置，所以必须先读配置）；
2. 起 trace_id 并绑到当前上下文。

**app 层与组合根的分工**（``src/composition/__init__.py``）：
组合根负责「配置 → 注册表 → 网关」这套**共享装配**；
app 层负责自己特有的东西 —— CLI 是日志与 trace_id，
server 会是 lifespan 与信号处理，MCP 会是会话管理。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from composition.bootstrap import Runtime, build_runtime
from foundation.ids import new_trace_id
from foundation.logging import set_trace_id, setup_logging
from foundation.settings import Settings, load_config

__all__ = ["open_runtime"]


def open_runtime(
    env: str | None = None,
    *,
    settings: Settings | None = None,
    trace_id: str | None = None,
    **kwargs: Any,
) -> Runtime:
    """装配运行时并初始化 CLI 的进程级状态。

    Args:
        env: 环境名；``None`` 时取 ``BESA_ENV``。
        settings: 已加载的配置（测试用）。
        trace_id: 显式指定（便于复现某次调用）；缺省新生成一个。
        **kwargs: 转交 :func:`composition.bootstrap.build_runtime`
            （测试用它注入 ``provider_options``）。
    """
    cfg = settings or load_config(env)

    # 日志级别在配置里，所以装日志必须晚于读配置
    setup_logging(cfg.get("logging.level"))
    set_trace_id(trace_id or new_trace_id())

    return build_runtime(cfg, **kwargs)


def runtime_env_vars(runtime: Runtime) -> Mapping[str, str]:
    """供 ``doctor`` 之类查看「到底读到了哪些环境变量名」（**不含值**）。"""
    return {name: "***" if value else "" for name, value in runtime.settings.env_vars.items()}

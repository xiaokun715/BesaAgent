"""统一日志配置：让「跑起来什么都看不见」变成「每个阶段都有进度」。

**为什么需要这个模块**：Python 的 ``logging`` 默认只把 WARNING 及以上输出到 stderr、
且不带格式 —— 于是代码里写的 ``logger.info(...)`` 全是**隐形**的。
「写了日志」和「看得到日志」之间，差的正是这一层配置。
besa-iv-kb 里 5 个模块都在用 ``logging``，却从没有人调过 ``basicConfig``。

各进程入口（``apps/cli`` / ``apps/server`` / ``apps/mcp``）调一次 :func:`setup_logging`
即可，**幂等**：重复调用只更新级别与 handler，不会叠加
（叠加的后果是一条日志打三遍，而人们会因此把日志级别调高，于是又什么都看不见了）。

**trace_id 进格式串**：``src/gateway`` 的一次调用横跨
路由 → 限流 → 重试 → 降级 → provider，一次失败可能连着换了 3 个模型。
没有 trace_id，日志就是一堆无法归因的孤立行。
本模块用 ``contextvars`` 承载它 —— **协程安全**，并发调用之间不会串。
（这是本包唯一允许依赖同仓库模块的地方，且只依赖 ``errors.redact_secrets``；
``observability.tracing`` 尚未实现，实现后按 ``foundation/__init__.py`` 的豁免接入。）

**脱敏**：装一个 :class:`_RedactFilter`，按 ``foundation.errors.redact_secrets``
统一过滤。API Key 绝不能出现在日志里（需求说明书-provider NFR-P-04）。
校验方式很粗暴但有效：**日志里出现 ``sk-`` 前缀即视为缺陷**。
"""

from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from foundation.errors import redact_secrets

__all__ = [
    "FORMAT",
    "current_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "setup_logging",
]

#: 默认格式。trace_id 放在中括号里靠前的位置 —— 排障时第一件事是按它过滤。
FORMAT = "%(asctime)s [%(levelname)-5s] [%(trace_id)s] %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

#: 当前协程上下文的 trace_id。
#:
#: 用 ``contextvars`` 而不是模块级全局变量：多 agent 场景下大量调用并发进行，
#: 全局变量会让 A 调用的 trace_id 出现在 B 调用的日志行上 ——
#: 那比没有 trace_id 更糟，因为它会把人引向错误的结论。
_TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("besa_trace_id", default="-")

#: 给 handler 打的标记，用于「只移除自己装的 handler」。
_OWNED = "_besa_owned_handler"


class _TraceIdFilter(logging.Filter):
    """把当前上下文的 trace_id 塞进每条记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 记录自己带了 trace_id 就尊重它（例如 gateway 在事件里显式传的）
        if not getattr(record, "trace_id", None):
            record.trace_id = _TRACE_ID.get()
        return True


class _RedactFilter(logging.Filter):
    """抹掉消息与参数里的疑似密钥。

    处理 ``record.msg`` 而不是格式化后的字符串，是因为 ``record.args``
    可能含敏感值（``logger.info("key=%s", key)``）——
    只处理 ``msg`` 会漏掉这一整类写法。

    **filter 盖不住异常回溯** —— 那部分由 ``Formatter`` 在输出时才格式化。
    所以必须同时用 :class:`_RedactingFormatter`，见它的说明。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and record.msg:
            record.msg = redact_secrets(record.msg)
        if record.args:
            record.args = _redact_args(record.args)
        return True


class _RedactingFormatter(logging.Formatter):
    """把脱敏延伸到**异常回溯与堆栈**。

    为什么需要它：``logging.Filter`` 只能改 ``record.msg`` / ``record.args``，
    而 ``exc_info`` 是在 ``Formatter.format()`` 里才被 ``formatException``
    展开成字符串的 —— 过滤链此时已经走完。

    这个缺口不是理论问题：``logger.exception`` 是**原始厂商报文最常见的泄漏路径**
    （适配器抛出带 401 响应体的错误 → 上层 ``logger.exception`` → 明文进日志）。
    只在 filter 里脱敏，等于给最常走的那扇门没上锁。
    """

    def formatException(self, ei: object) -> str:  # type: ignore[override]
        return redact_secrets(super().formatException(ei))  # type: ignore[arg-type]

    def formatStack(self, stack_info: str) -> str:
        return redact_secrets(super().formatStack(stack_info))


def _redact_args(args: Any) -> Any:
    if isinstance(args, Mapping):
        return {key: _redact_value(value) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_value(value) for value in args)
    return _redact_value(args)


def _redact_value(value: Any) -> Any:
    return redact_secrets(value) if isinstance(value, str) else value


def set_trace_id(trace_id: str) -> contextvars.Token[str]:
    """把 trace_id 绑到当前上下文。返回的 token 交给 :func:`reset_trace_id`。"""
    return _TRACE_ID.set(trace_id or "-")


def reset_trace_id(token: contextvars.Token[str]) -> None:
    """恢复上一个 trace_id。**必须与 :func:`set_trace_id` 成对**，否则会串到调用方。"""
    _TRACE_ID.reset(token)


def current_trace_id() -> str:
    return _TRACE_ID.get()


def setup_logging(
    level: int | str | None = None,
    *,
    stream: TextIO | None = None,
) -> None:
    """配置根 logger。**幂等**。

    Args:
        level: 级别名或数值。``None`` 时取 ``BESA_LOG_LEVEL``，再缺省 ``INFO``。
        stream: 输出目标。默认 stderr —— 不占 stdout，这样
            ``besa runtime "问题" > answer.txt`` 能拿到干净的回答。

    **只移除自己上次装的 handler**，不动调用方（或测试框架）装的 ——
    ``logging.basicConfig`` 式的「清空重来」会让 pytest 的 caplog 失效。
    """
    root = logging.getLogger()
    resolved = _resolve_level(level)

    for handler in list(root.handlers):
        if getattr(handler, _OWNED, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_RedactingFormatter(FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(_TraceIdFilter())
    handler.addFilter(_RedactFilter())
    setattr(handler, _OWNED, True)

    root.addHandler(handler)
    root.setLevel(resolved)

    # httpx 每个请求打一条 INFO，会淹没有用信息
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _resolve_level(level: int | str | None) -> int:
    if isinstance(level, int):
        return level

    name = level
    if name is None:
        import os

        name = os.environ.get("BESA_LOG_LEVEL", "INFO")

    resolved = logging.getLevelName(str(name).upper())
    # getLevelName 对未知名字返回 "Level X" 字符串 —— 静默退回 INFO，
    # 但**不吞掉**这个事实：写错级别名的人应当在启动日志里看到提示。
    if not isinstance(resolved, int):
        logging.getLogger(__name__).warning("未知的日志级别 %r，回退到 INFO", name)
        return logging.INFO
    return resolved

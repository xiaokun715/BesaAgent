"""UUID 与标识符收敛助手。

**问题**：PG 里 ``session_id`` / ``event_id`` / ``memory_id`` 等列都是 ``uuid`` 类型，
而业务层到处在用 ``str``。直接透传非 UUID 字符串（测试里的 ``"s-1"``、
占位调用方的 ``"u1"``）只会让 asyncpg 抛::

    invalid UUID 'u1': length must be between 32..36 characters

这类错误发生在**驱动层**，堆栈里看不到调用方是谁，排查成本极高。
besa-iv-kb 里这段逻辑曾被复制在 4 个文件中，随统一 DB 访问层一并收敛。

**为什么 trace_id 必须只有一份生成函数**：它是**横切**的 ——
``logging`` 要把它盖进格式串、``gateway`` 要跨模型透传、``events`` 要携带、
``repo`` 要落库。四个地方各自 ``uuid4()`` 的结果是四套无法关联的标识，
而「一次失败横跨 3 个模型」正是最需要关联的场景。

**本模块只做生成与收敛，不做校验策略决策**（比如「非法 ID 该报错还是当 None」）——
那由调用方决定，因为它取决于该字段在业务上是否可空。
"""

from __future__ import annotations

import uuid

__all__ = ["new_id", "new_trace_id", "to_uuid"]

#: trace_id 用短横线全展开的十六进制（32 字符，无连字符）。
#:
#: 不用标准 UUID 形式是刻意的：它更短、更容易在日志里一眼扫到，
#: 而且避免了「trace_id 被误当成主键」这种混淆 —— 它只是个关联标记。
_TRACE_LENGTH = 32


def new_id() -> uuid.UUID:
    """生成一个业务主键。"""
    return uuid.uuid4()


def new_trace_id() -> str:
    """生成一个 trace_id。

    **一次调用只有一个**，从入口（CLI / HTTP / MCP）生成后向下透传，
    中途任何一层都不得重新生成 —— 重新生成等于把调用链切断。
    """
    return uuid.uuid4().hex[:_TRACE_LENGTH]


def to_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    """把宽松输入收敛成 UUID；``None`` 原样返回。

    Raises:
        ValueError: 无法解析。错误信息里带上**原始值** ——
            否则调用方只知道「有个 ID 不合法」，不知道是哪个。
    """
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(
            f"{value!r} 不是合法的 UUID。"
            "若这是测试里的占位值（如 'u1'），请改用 uuid4() 生成的合法值。"
        ) from exc

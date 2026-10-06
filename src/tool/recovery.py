"""异常分类与自愈（`FR-T-17`）。

**「失败就抛回给模型」是必要但不充分的。** 它的问题是：
不同性质的失败被一视同仁地回给模型，于是模型只能靠猜 ——
而其中有一类（参数错）它**大概率能改对**，只要告诉它改哪里。

## 自愈按类别分级，**不全自动**（`DT-16`）

=================  ========================  ============================================
失败类别            动作                       为什么
=================  ========================  ============================================
``param``          ``advise``：给出**具体位置**  「schema 不符」模型改不动；「第 3 个参数缺
                                              ``path``」它能改
``transient``      ``retry_once`` —— **仅当**     网络抖动能自愈；但有副作用且状态不确定时
                   该工具幂等安全时              自动重试 = 可能重复执行
``environment``    ``switch`` / ``advise``      命令不存在、目录只读 —— 换等价工具，或明说
``fatal``          ``report``                  权限被永久拒绝，重试一万次也一样
=================  ========================  ============================================

## 独立于幂等的一道防线：无效重试熔断

幂等管的是「**同一个键**」。而模型很可能每次都生成一份**略微不同**的参数
（多一个空格、换个路径写法）—— 于是键不同、幂等不生效，
而它在做的是**同一件注定失败的事**。

熔断按**参数指纹**计数（与幂等同一个口径，见 ``idempotency.args_digest``）：
同一个工具 + 同一份参数连续失败 N 次之后，从第 N+1 次起直接拒绝并让模型换做法。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from tool.types import ERROR_KINDS, ErrorKind, RecoveryAction, SideEffect

__all__ = ["DEFAULT_REFUSE_AFTER", "Recovery", "RetryBreaker", "classify"]

_log = logging.getLogger(__name__)

#: 同一份参数连续失败多少次之后开始拒绝。
#: **默认 3**：给模型两次「改了但还是不对」的机会，第三次说明它没在改对方向。
DEFAULT_REFUSE_AFTER = 3


@dataclass(frozen=True)
class Recovery:
    """一次失败的处置建议。"""

    kind: ErrorKind = "unknown"
    action: RecoveryAction = "report"
    #: 给模型的**可操作**提示。空表示没什么可说的。
    hint: str = ""

    @property
    def auto_retry(self) -> bool:
        return self.action == "retry_once"


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #

#: 从错误文本里认类别。**顺序有意义** —— 先匹配到的算。
#:
#: 为什么用文本而不是异常类型：本模块抛出的错误**有一半是我们自己拼的消息**
#: （schema 不符、超出范围、被策略拒绝），它们没有专属的异常类型。
#: 用类型判会让这一半落到 ``unknown`` 里，而那一类恰恰是最该被区分的。
_SIGNATURES: tuple[tuple[ErrorKind, tuple[str, ...]], ...] = (
    ("param", ("schema", "参数", "缺少必需参数", "不能为空", "未知的 mode")),
    ("param", ("超出", "不在", "范围", "path_traversal", "denied_path")),
    ("transient", ("超时", "timeout", "timed out", "连接", "connection", "502", "503", "504")),
    ("environment", ("not found", "不存在", "只读", "read-only", "permission denied")),
    ("fatal", ("未授权", "未启用", "拒绝执行", "refused", "循环", "深度")),
)


def classify(
    error: BaseException | None = None,
    *,
    message: str = "",
    side_effect: SideEffect = "read",
) -> Recovery:
    """把一次失败归类，并给出**敢不敢自动重试**。

    Args:
        error: 抛出的异常（若有）。
        message: 错误文本。
        side_effect: 出错的那个工具的副作用等级 —— **它决定能不能自动重试**。
    """
    text = f"{message or ''} {error or ''}".lower()

    kind: ErrorKind = "unknown"
    for candidate, needles in _SIGNATURES:
        if any(needle.lower() in text for needle in needles):
            kind = candidate
            break

    if kind == "unknown" and error is not None:
        # 类型兜底：文本没认出来时再看类型。放后面是因为文本更具体。
        name = type(error).__name__
        if isinstance(error, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            kind = "environment"
        elif isinstance(error, PermissionError):
            kind = "fatal"
        elif isinstance(error, (TimeoutError, ConnectionError)):
            kind = "transient"
        elif isinstance(error, (ValueError, TypeError)):
            kind = "param"
        elif name:  # pragma: no cover - 只用于排障
            _log.debug("未能归类的工具错误：%s", name)

    if kind not in ERROR_KINDS:  # pragma: no cover - 防御性
        kind = "unknown"

    # ---- 动作 ----
    if kind == "param":
        return Recovery(
            kind="param",
            action="advise",
            hint="按上面的信息改参数后重试；参数错误重试同样的值不会有不同结果。",
        )

    if kind == "transient":
        if side_effect == "read":
            return Recovery(
                kind="transient", action="retry_once", hint="疑似瞬时故障，已自动重试一次。"
            )
        # **有副作用的不自动重试**：`FR-T-08` 的「不确定」状态下重试可能重复执行。
        return Recovery(
            kind="transient",
            action="advise",
            hint=(
                f"疑似瞬时故障，但 {side_effect!r} 类工具**不会自动重试** —— "
                "重复执行可能产生第二份副作用。要重试请显式再调一次。"
            ),
        )

    if kind == "environment":
        return Recovery(
            kind="environment",
            action="switch",
            hint="执行环境不满足（命令/路径/权限）。考虑换一个等价工具，或换一个目标。",
        )

    if kind == "fatal":
        return Recovery(kind="fatal", action="report", hint="这类失败重试不会有不同结果。")

    return Recovery(kind="unknown", action="report")


# --------------------------------------------------------------------------- #
# 无效重试熔断
# --------------------------------------------------------------------------- #


@dataclass
class RetryBreaker:
    """按**参数指纹**记连续失败 —— 与幂等同一个口径。

    **为什么需要它**（幂等盖不住的那一类）：模型很可能每次都生成一份
    **略微不同**的参数，于是幂等键不同、去重不生效，
    而它在做的是同一件注定失败的事 —— 只是白烧调用次数。

    **作用域是进程内**：跨进程共享它需要 Redis，而这条防线要拦的是
    「同一个 agent 在几秒内反复撞同一堵墙」—— 那发生在一个进程里。
    跨进程的无效重试由 ``max_calls_per_run`` 兜底。
    """

    refuse_after: int = DEFAULT_REFUSE_AFTER

    _failures: dict[str, int] = field(default_factory=dict)

    def record_failure(self, fingerprint: str) -> int:
        """记一次失败，返回**已经连续失败了几次**。"""
        count = self._failures.get(fingerprint, 0) + 1
        self._failures[fingerprint] = count
        return count

    def should_refuse(self, fingerprint: str) -> bool:
        """这次该不该直接拒绝。"""
        return self.count(fingerprint) >= self.refuse_after

    def count(self, fingerprint: str) -> int:
        """某个指纹当前连续失败了几次。"""
        return self._failures.get(fingerprint, 0)

    def clear(self, fingerprint: str) -> None:
        """成功了，清掉计数。

        **必须清** —— 不清的话，一个工具在若干次偶发失败之后会被永久拉黑，
        而那看起来会像「这个工具坏了」。
        """
        self._failures.pop(fingerprint, None)

    def reset(self) -> None:
        self._failures.clear()

    def describe(self) -> dict[str, Any]:
        """给 ``doctor`` / 排障用：现在有哪些指纹在连着失败。"""
        return {k: v for k, v in self._failures.items() if v >= self.refuse_after}

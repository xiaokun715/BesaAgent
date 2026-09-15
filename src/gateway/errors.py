"""gateway 的错误语义。

**为什么新建这个文件**（对《架构概要设计-gateway》§8 D-A 的补充）：
那份文档只提到缺 ``types.py``，但 gateway 同样缺错误定义 ——
``AllCandidatesFailedError`` 这类错误既不是数据类型（不属于 ``types.py``），
也不能留在 ``gateway.py``（那是编排文件）。

按 ``src/provider/errors.py`` 的**对称结构**新增本文件，
是让两层「契约 + 错误 + 类型」三件套保持一致的最小改动。

**与 provider 错误的分工**（``foundation/errors.py`` 的三层错误模型）：

    provider 表达「这次调用为什么失败」（带 retryable）
    gateway  表达「**所有**候选都失败了 / 预算耗尽 / 没有能胜任的模型」

上层的业务代码只该看见第二层。它不该知道「m1 是 503、m2 是 429」——
那是 gateway 排障时通过 ``attempts`` 暴露的**诊断信息**，不是业务分支条件。
"""

from __future__ import annotations

from collections.abc import Sequence

from gateway.types import AttemptRecord

__all__ = [
    "AllCandidatesFailedError",
    "BudgetExhaustedError",
    "GatewayError",
    "NoCapableModelError",
    "StreamCommittedError",
    "UnknownAliasError",
]


class GatewayError(Exception):
    """gateway 错误的基类。"""

    def __init__(
        self,
        message: str,
        *,
        alias: str = "",
        trace_id: str = "",
        attempts: Sequence[AttemptRecord] = (),
    ) -> None:
        self.message = message
        self.alias = alias
        self.trace_id = trace_id
        self.attempts = tuple(attempts)
        super().__init__(message)

    def summary(self) -> str:
        """把尝试链压成一行摘要 —— 排障时最需要的正是这个。

        只报「全部候选失败」而不给每个候选的原因，等于什么都没说：
        排查者只能重新跑一遍并加日志。
        """
        if not self.attempts:
            return ""
        parts = []
        for record in self.attempts:
            if record.skipped:
                parts.append(f"{record.model_key}:跳过({record.skipped_reason})")
            else:
                parts.append(f"{record.model_key}:{record.error or record.outcome}")
        return " → ".join(parts)

    def __str__(self) -> str:
        parts = [self.message]
        if self.alias:
            parts.append(f"alias={self.alias}")
        detail = self.summary()
        if detail:
            parts.append(f"[{detail}]")
        if self.trace_id:
            parts.append(f"trace={self.trace_id}")
        return " ".join(parts)


class UnknownAliasError(GatewayError):
    """逻辑模型名未注册。

    ``FR-G-02`` 验收点要求错误信息**列出可用逻辑名** ——
    拼错 alias 是最常见的配置错误，而只报「未注册」会让人去翻文档。
    """

    def __init__(self, alias: str, *, known: Sequence[str] = (), trace_id: str = "") -> None:
        listing = ", ".join(known) if known else "（无）"
        super().__init__(
            f"未知的逻辑模型名 {alias!r}；可用的逻辑名：{listing}",
            alias=alias,
            trace_id=trace_id,
        )
        self.known = tuple(known)


class NoCapableModelError(GatewayError):
    """**没有任何候选**满足本次请求的能力要求（``FR-G-03``）。

    这与 :class:`AllCandidatesFailedError` 不同：那些候选**根本没被调用过** ——
    这是配置问题，不是运行时故障，重试没有任何意义。
    """


class BudgetExhaustedError(GatewayError):
    """预算耗尽（``FR-G-12``）。

    ``reason`` 区分 ``attempts`` 与 ``deadline``，因为两者的修法完全不同：
    前者调大 ``total_max_attempts``，后者调大 ``deadline``。
    """

    def __init__(
        self,
        *,
        reason: str,
        alias: str = "",
        attempts: Sequence[AttemptRecord] = (),
        trace_id: str = "",
    ) -> None:
        hint = {
            "attempts": "上游调用次数已达上限，可调大 gateway.retry.total_max_attempts",
            "deadline": "总超时预算已耗尽，可调大 gateway.deadline.default_s",
        }.get(reason, reason)
        super().__init__(
            f"调用预算耗尽（{reason}）：{hint}",
            alias=alias,
            trace_id=trace_id,
            attempts=attempts,
        )
        self.reason = reason


class AllCandidatesFailedError(GatewayError):
    """所有候选都试过了，全部失败。

    **与「某个模型失败」是两件事**：前者是业务该处理的，后者是内部细节。
    上层的 ``except`` 只该捕获这一个。
    """


class StreamCommittedError(GatewayError):
    """流式已经输出过正文，无法再重试或降级（``FR-G-05``）。

    这不是「失败」，而是「无法挽回的失败」—— 用户已经看到了半截输出。
    单独成类是为了让调用方能区分它并做出不同的交互（例如提示「输出中断」而不是「请重试」）。
    """

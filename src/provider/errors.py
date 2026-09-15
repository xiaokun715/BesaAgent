"""厂商调用失败的错误树。

**这一层存在的意义**：把各厂商形态各异的失败（状态码 / 错误体 / 连接异常）
归一成**可编程判定**的一棵树。上层唯一需要读的字段是 ``retryable`` ——
``src/gateway`` 的全部重试决策都建立在它之上（FR-P-09 / FR-G-04）。

**与 ``foundation.errors`` 的分工**：那边是**跨模块通用**错误（参数非法 / 未找到 / 超时），
这边是**厂商调用**错误，带「是否可重试」语义。这条语义**刻意不上升到 foundation** ——
它只有在这里才成立（只有适配层知道厂商状态码的含义）。

**``CancelledError`` 不在这棵树里**（FR-P-14）：它是 ``asyncio.CancelledError``，
必须**原样传播**。适配层的任何 ``except`` 都不得捕获 ``BaseException``，
否则「用户点了取消」会被伪装成「模型调用失败」，调用方再也无法区分这两件事。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from foundation.errors import redact_secrets

__all__ = [
    "AuthError",
    "CapabilityNotSupportedError",
    "ContentFilteredError",
    "ContextLengthError",
    "InvalidRequestError",
    "ModelNotFoundError",
    "NetworkError",
    "ProtocolError",
    "ProviderError",
    "RateLimitError",
    "TimeoutError",
    "UnknownProviderError",
    "UpstreamError",
    "map_http_status",
    "parse_retry_after",
]

#: 保留的厂商原始报文长度。够排障，又不会把日志冲爆。
_RAW_LIMIT: int = 500


class ProviderError(Exception):
    """所有厂商调用失败的基类。

    **消息里绝不出现 API Key**（NFR-P-04）。构造时会自动过一遍
    :func:`foundation.errors.redact_secrets` —— 依赖「调用方记得脱敏」是不可靠的，
    而这里正是原始厂商报文的唯一入口。
    """

    #: 是否值得重试。**gateway 唯一需要读的字段。**
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        model: str = "",
        status_code: int | None = None,
        raw: str | None = None,
        trace_id: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.message = redact_secrets(str(message))
        self.provider = provider
        self.model = model
        self.status_code = status_code
        # FR-P-09：不吞原始信息。截断但保留 —— 没有它，新出现的厂商错误无从下手。
        self.raw = redact_secrets(raw)[:_RAW_LIMIT] if raw else None
        self.trace_id = trace_id
        self.retry_after = retry_after
        super().__init__(self.message)

    # ---------------------------------------------------------------- 展示
    def __str__(self) -> str:
        parts = [self.message]
        where = "/".join(x for x in (self.provider, self.model) if x)
        if where:
            parts.append(f"[{where}]")
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.trace_id:
            parts.append(f"trace={self.trace_id}")
        return " ".join(parts)

    def __repr__(self) -> str:
        # 刻意不输出 raw：repr 会进日志与调试器，是最常见的泄漏路径之一。
        return (
            f"{type(self).__name__}(message={self.message!r}, "
            f"provider={self.provider!r}, model={self.model!r}, "
            f"status_code={self.status_code!r}, retryable={self.retryable})"
        )

    # ---------------------------------------------------------------- 转换
    def with_trace(self, trace_id: str) -> ProviderError:
        """补上 trace_id。适配层常常在更内层才知道它，用这个方法回填而不必重建异常。"""
        if self.trace_id or not trace_id:
            return self
        self.trace_id = trace_id
        return self


# --------------------------------------------------------------------------- #
# 不可重试（重试只是白烧配额）
# --------------------------------------------------------------------------- #


class AuthError(ProviderError):
    """密钥无效 / 无权限。**不重试** —— 重试多少次都是 401。"""

    retryable: ClassVar[bool] = False


class InvalidRequestError(ProviderError):
    """请求参数非法。**不重试** —— 同样的请求重发结果必然相同。"""

    retryable: ClassVar[bool] = False


class ContextLengthError(InvalidRequestError):
    """上下文超长。

    单列出来是因为它是 agent 场景的**常见**失败，且对策**不是**重试也不是降级，
    而是缩小上下文（``src/context`` 的压缩）。gateway 需要能识别它并给出这个提示，
    否则排障方向会跑偏成「换个模型试试」。
    """

    retryable: ClassVar[bool] = False


class ModelNotFoundError(ProviderError):
    """模型名错 / 未开通。**不重试**。"""

    retryable: ClassVar[bool] = False


class ContentFilteredError(ProviderError):
    """被安全策略拒绝。**不重试** —— 同样的输入必然被同样拒绝。"""

    retryable: ClassVar[bool] = False


class ProtocolError(ProviderError):
    """响应不是合法 JSON / 缺字段。

    默认**不重试**：协议异常通常意味着端点形状变了（配置指错了地址、
    或者用了不兼容的兼容层），重试不会自愈。
    """

    retryable: ClassVar[bool] = False


class UnknownProviderError(ProviderError):
    """无法归类。**不重试**，且必须保留原始报文 —— 这类错误正是新情况的入口。"""

    retryable: ClassVar[bool] = False


# --------------------------------------------------------------------------- #
# 可重试
# --------------------------------------------------------------------------- #


class RateLimitError(ProviderError):
    """限流 / 配额耗尽（429）。

    ``retry_after`` 来自上游 ``Retry-After`` 头。**上游给了就遵守它** ——
    自己拍一个退避时长，通常在配额真正恢复前就重新撞上去。
    """

    retryable: ClassVar[bool] = True


class UpstreamError(ProviderError):
    """上游故障（5xx）。

    provider **只上抛**，不自行重试：厂商故障需要**跨模型**决策，
    换一个模型往往比锤同一个更有用（架构概要设计-provider §4.2）。
    """

    retryable: ClassVar[bool] = True


class NetworkError(ProviderError):
    """连接失败。请求**大概率没到达模型**，是最适合本地重试的一类。"""

    retryable: ClassVar[bool] = True


class TimeoutError(ProviderError):  # noqa: A001 - 刻意与内置名区分：这是**上游调用**超时
    """读超时。同样适合本地重试，但要受总 deadline 约束（FR-P-12）。"""

    retryable: ClassVar[bool] = True


# --------------------------------------------------------------------------- #
# 本地错误（根本没发出去 / 不该发出去）
# --------------------------------------------------------------------------- #


class CapabilityNotSupportedError(ProviderError):
    """本地能力拦截：请求了模型不具备的能力（FR-P-08）。

    **刻意也继承 ProviderError**，尽管它并非「厂商失败」——
    这样 gateway 只需一个 ``except ProviderError`` 就能兜住全部失败路径。
    分类学上的纯粹性不值得让调用方多写一个 except 分支。

    ``retryable=False``：换个候选**可能**有救，但那是 gateway 的降级决策，
    不是「重试同一个模型」。
    """

    retryable: ClassVar[bool] = False


# --------------------------------------------------------------------------- #
# 状态码映射
# --------------------------------------------------------------------------- #

#: 上下文超长的常见报文特征。厂商各写各的，只能靠特征词。
#: 宁可漏判（退化成 InvalidRequestError）也不要误判 —— 误判会给出错误的排障方向。
_CONTEXT_HINTS: tuple[str, ...] = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "context window",
)


def map_http_status(
    status_code: int,
    *,
    body: str = "",
    provider: str = "",
    model: str = "",
    trace_id: str = "",
    retry_after: float | None = None,
) -> ProviderError:
    """HTTP 状态码 + 响应体 → 错误对象。

   这是适配层**唯一**的「翻译」动作，契约见《架构概要设计-provider》§2.2 的映射表。

    Args:
        status_code: HTTP 状态码。
        body: 响应体文本。会被脱敏并截断后放进 ``raw``。
        retry_after: 上游 ``Retry-After`` 头解析出的秒数（仅 429 有意义）。

    Returns:
        对应的 :class:`ProviderError` 子类实例。
    """
    context = {"provider": provider, "model": model, "trace_id": trace_id, "raw": body or None}
    lowered = (body or "").lower()

    if status_code in (401, 403):
        return AuthError("鉴权失败：密钥无效或无权限", status_code=status_code, **context)

    if status_code == 404:
        return ModelNotFoundError(
            "模型不存在或未开通", status_code=status_code, **context
        )

    if status_code == 413 or (status_code == 400 and any(h in lowered for h in _CONTEXT_HINTS)):
        return ContextLengthError(
            "上下文超长：需要缩小输入（压缩上下文或减少历史），换模型不会解决",
            status_code=status_code,
            **context,
        )

    if status_code == 429:
        return RateLimitError(
            "上游限流或配额耗尽",
            status_code=status_code,
            retry_after=retry_after,
            **context,
        )

    if status_code == 408:
        return TimeoutError("上游超时", status_code=status_code, **context)

    if 400 <= status_code < 500:
        return InvalidRequestError(
            "请求参数非法", status_code=status_code, **context
        )

    if 500 <= status_code < 600:
        return UpstreamError(
            "上游故障", status_code=status_code, **context
        )

    return UnknownProviderError(
        f"未归类的 HTTP 状态码 {status_code}", status_code=status_code, **context
    )


def parse_retry_after(headers: Mapping[str, Any]) -> float | None:
    """从响应头解析 ``Retry-After``。只支持秒数形式，HTTP-date 形式忽略。

    忽略 HTTP-date 是刻意的：厂商极少用那种形式，而解析错日期会得到一个
    离谱的等待时长（可能是几分钟），比不解析更糟。
    """
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None

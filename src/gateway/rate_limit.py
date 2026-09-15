"""限流：RPM / TPM / 并发数。

**三个维度的实现要点各不相同**：

============  ==============================================================
维度           要点
============  ==============================================================
``rpm``        60 秒滑动窗口。用**时间戳队列**而不是固定窗口计数器 ——
               固定窗口在边界处会放行接近 2 倍配额（前 1 秒 + 后 1 秒）。
``tpm``        **按预估预扣、按实际回补**。不这么做的话，
               配额永远是「上一次请求**之后**」的视角，而一个请求可能吃几千 token ——
               等它回来时，超发的量已经发出去了。所以 :meth:`reconcile` 是必需的，
               不是优化。
``concurrency`` 在途请求数。**取消时必须释放**（``FR-G-13`` 验收点要求配额归零）——
               泄漏一个并发额度意味着那个模型永久少一个并发位。
============  ==============================================================

**多进程**：本模块只实现**进程内**后端。跨进程需要 Redis（``D-4``），
配置成 ``backend: redis`` 时会**明确报错**而不是静默退化成单进程限流 ——
静默退化的后果是生产环境上配额实际超发 N 倍（N = 进程数），而本地测试完全正常。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from foundation.clock import Clock, SystemClock
from gateway.retry import CallBudget

__all__ = [
    "LocalRateLimiter",
    "RateLimitDecision",
    "RateLimitPolicy",
    "build_rate_limiter",
]

_log = logging.getLogger(__name__)

#: 滑动窗口长度。RPM / TPM 都是「每分钟」。
WINDOW_S = 60.0

#: 并发维度不可等待时的重试间隔。并发约束是「等一个在途请求结束」，
#: 没有可计算的时间点，只能短轮询；外层用 ``max_wait_s`` 兜住上界。
_CONCURRENCY_POLL_S = 0.05


@dataclass(frozen=True)
class RateLimitPolicy:
    """单个模型的限额。``None`` = 不限制该维度。"""

    rpm: int | None = None
    tpm: int | None = None
    max_concurrency: int | None = None

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> RateLimitPolicy:
        data = dict(cfg or {})
        return cls(
            rpm=_opt_int(data.get("rpm")),
            tpm=_opt_int(data.get("tpm")),
            max_concurrency=_opt_int(data.get("max_concurrency")),
        )


@dataclass(frozen=True)
class RateLimitDecision:
    """一次配额申请的结果。"""

    allowed: bool
    reason: str | None = None
    #: 预计多久后会有额度释放（秒）。等待策略据此计算 sleep 时长。
    retry_after_s: float = 0.0

    @property
    def denied(self) -> bool:
        return not self.allowed


class LocalRateLimiter:
    """进程内限流器。

    **不做跨进程同步** —— 见模块 docstring。多进程部署时必须换 Redis 后端。
    """

    def __init__(
        self,
        policy_for: Callable[[str], RateLimitPolicy] | RateLimitPolicy,
        *,
        clock: Clock | None = None,
        on_exceed: str = "wait",
    ) -> None:
        """
        Args:
            policy_for: 按模型键取限额；也可直接给一个统一限额。
            on_exceed: ``"wait"``（排队等待，默认）或 ``"fallback"``（立即换候选）。
                ``wait`` 为默认，因为限流通常是**短暂**的（下一秒额度就回来了），
                而换模型是**永久性**代价（更贵 / 更弱）。但等待**必须**受预算约束 ——
                见 :meth:`acquire`。
        """
        if on_exceed not in ("wait", "fallback"):
            raise ValueError(f"on_exceed 必须是 'wait' 或 'fallback'，得到 {on_exceed!r}")

        self._policy_for: Callable[[str], RateLimitPolicy] = (
            (lambda _key: policy_for) if isinstance(policy_for, RateLimitPolicy) else policy_for
        )
        self._clock: Clock = clock or SystemClock()
        self._on_exceed = on_exceed

        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._tokens: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self._inflight: dict[str, int] = defaultdict(int)

    # ---------------------------------------------------------------- 申请
    async def acquire(
        self,
        key: str,
        *,
        estimated_tokens: int = 0,
        budget: CallBudget | None = None,
    ) -> RateLimitDecision:
        """申请一次调用配额。**成功时必须由调用方在 finally 里 release**。

        等待时长受 ``budget.remaining_s()`` 约束：**等一个必然超时的队是纯粹的浪费**，
        此时应当改判为「换候选」（``架构概要设计-gateway`` §4.6）。

        Args:
            key: 模型键。
            estimated_tokens: 本次请求预计消耗的 token（用于 TPM 预扣）。
                粗估即可 —— 见 ``D-D``：一期用字符数估算，不引入 tokenizer 依赖。
            budget: 总预算。为 ``None`` 时不等待（等价于立即判定）。

        Returns:
            :class:`RateLimitDecision`。``allowed=False`` 时 ``reason`` 指出是哪个维度。
        """
        waited = 0.0

        while True:
            decision = self._try_acquire(key, estimated_tokens)
            if decision.allowed:
                return decision

            # concurrency 维度是「等一个在途请求结束」，没有可计算的时间点
            wait_s = decision.retry_after_s
            if wait_s <= 0:
                wait_s = _CONCURRENCY_POLL_S

            if self._on_exceed != "wait":
                return decision

            if budget is None:
                return decision

            remaining = budget.remaining_s()
            # 用 `>=` 而不是 `>`：**恰好等到 deadline 才拿到额度**同样是没用的 ——
            # 拿到额度的那一刻预算已经归零，这次调用必然被 ``try_acquire`` 拒掉。
            # 白等一轮再失败，不如立刻换候选。
            if remaining <= 0 or waited + wait_s >= remaining:
                return RateLimitDecision(
                    decision.allowed, decision.reason, retry_after_s=wait_s
                )

            await self._clock.sleep(wait_s)
            waited += wait_s

    def _try_acquire(self, key: str, estimated_tokens: int) -> RateLimitDecision:
        policy = self._policy_for(key)
        now = self._clock.monotonic()
        self._prune(key, now)

        if policy.max_concurrency is not None:
            if self._inflight[key] >= policy.max_concurrency:
                return RateLimitDecision(False, "concurrency", _CONCURRENCY_POLL_S)

        if policy.rpm is not None:
            requests = self._requests[key]
            if len(requests) >= policy.rpm:
                # 最早那次请求滑出窗口时，就会有额度释放
                return RateLimitDecision(False, "rpm", requests[0] + WINDOW_S - now)

        if policy.tpm is not None:
            tokens = self._tokens[key]
            used = sum(count for _ts, count in tokens)
            if used + estimated_tokens > policy.tpm:
                if not tokens:
                    # 单次请求就超过整分钟的配额：等待也没有意义，直接拒绝。
                    # 这通常意味着 tpm 配错了，而不是「稍后就好了」。
                    return RateLimitDecision(False, "tpm", retry_after_s=float("inf"))
                return RateLimitDecision(False, "tpm", tokens[0][0] + WINDOW_S - now)

        # 授予：三个维度一起记账。**必须同时记**，否则会出现
        # 「RPM 过了但 TPM 没记」→ 下一轮 TPM 少算一次。
        self._requests[key].append(now)
        if estimated_tokens:
            self._tokens[key].append((now, estimated_tokens))
        self._inflight[key] += 1
        return RateLimitDecision(True)

    # ---------------------------------------------------------------- 释放
    def release(self, key: str) -> None:
        """释放并发额度。**必须幂等**，且在取消路径上也要被调用。"""
        if self._inflight.get(key, 0) > 0:
            self._inflight[key] -= 1

    def reconcile(self, key: str, *, estimated_tokens: int, actual_tokens: int | None) -> None:
        """用**实际**用量修正预扣额度。

        ``TPM`` 预扣的意义是「在请求发出前就占住配额」，但预扣值必然不准。
        不修正的后果是配额的占用只增不减 —— 跑一段时间后 TPM 会被永久占满，
        表现为「明明没打满却一直限流」。

        ``actual_tokens`` 为 ``None``（上游未返回用量）时**什么都不做** ——
        猜一个值修正，比不修正更糟。
        """
        if actual_tokens is None or not estimated_tokens:
            return
        delta = actual_tokens - estimated_tokens
        if delta == 0:
            return

        tokens = self._tokens.get(key)
        if not tokens:
            return
        timestamp, count = tokens[-1]
        tokens[-1] = (timestamp, max(0, count + delta))

    # ---------------------------------------------------------------- 内部
    def _prune(self, key: str, now: float) -> None:
        cutoff = now - WINDOW_S
        requests = self._requests.get(key)
        if requests:
            while requests and requests[0] <= cutoff:
                requests.popleft()
        tokens = self._tokens.get(key)
        if tokens:
            while tokens and tokens[0][0] <= cutoff:
                tokens.popleft()

    def snapshot(self) -> dict[str, Any]:
        """给健康页/排障用。"""
        return {
            "inflight": dict(self._inflight),
            "requests_in_window": {key: len(value) for key, value in self._requests.items()},
        }


def build_rate_limiter(
    cfg: Mapping[str, Any] | None,
    *,
    clock: Clock | None = None,
) -> LocalRateLimiter:
    """按配置构造限流器。

    **目前只有本地后端。** 配 ``backend: redis`` 会**明确报错**而不是静默退化成
    单进程限流 —— 后者在生产上的表现是配额超发 N 倍（N = 进程数），
    而本地测试**完全正常**，是最难排查的一类问题。

    宁可让多进程部署在上线前就撞到这堵墙，也不要让它上线后才发现。
    """
    data = dict(cfg or {})
    backend = str(data.get("backend", "local")).strip().lower()

    if backend not in ("local", "memory"):
        raise NotImplementedError(
            f"限流后端 {backend!r} 尚未实现。当前仅支持进程内后端（'local'／'memory'）。\n"
            "跨进程限流（Redis）见《架构概要设计-gateway》§8 D-4，属二期。\n"
            "注意：多进程部署下使用本地后端会让实际配额**超发 N 倍**（N = 进程数）。"
        )

    defaults = data.get("defaults") or {}
    return LocalRateLimiter(
        RateLimitPolicy.from_config(defaults),
        clock=clock,
        on_exceed=str(data.get("on_exceed", "wait")),
    )


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)

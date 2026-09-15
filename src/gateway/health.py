"""熔断器：把确认挂掉的模型**摘出去**，不再浪费预算。

**为什么必须在 ``retry`` 之前**（架构概要设计-gateway §2.1）：
对一个已经连续失败 5 次的模型重试三次，比「浪费三次请求」更糟 ——
它会挤占 ``CallBudget``，导致本该降级到健康模型的请求**直接超时**。
所以熔断拦截不消耗重试次数，它发生在配额申请之前。

**半开期用「任一失败」而非失败率**：默认只有 2 个探测样本，
算比率没有统计意义。用「任一失败」更保守也更简单 ——
多探测一轮的成本，远小于把一个不稳定的模型重新放进主链的代价。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from foundation.clock import Clock, SystemClock

__all__ = ["CircuitBreaker", "CircuitState", "HealthPolicy", "HealthRegistry"]

_log = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"          # 正常放行
    OPEN = "open"              # 熔断中，直接跳过
    HALF_OPEN = "half_open"    # 冷却到期，少量放行探测


@dataclass(frozen=True)
class HealthPolicy:
    """熔断参数。全部外置可配。"""

    failure_threshold: int = 5
    cooldown_s: float = 30.0
    half_open_probes: int = 2

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold 必须 >= 1")
        if self.half_open_probes < 1:
            raise ValueError("half_open_probes 必须 >= 1")

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> HealthPolicy:
        data = dict(cfg or {})
        return cls(
            failure_threshold=int(data.get("failure_threshold", 5)),
            cooldown_s=float(data.get("cooldown_s", 30.0)),
            half_open_probes=int(data.get("half_open_probes", 2)),
        )


class CircuitBreaker:
    """单个模型的熔断器。

    状态机::

        CLOSED --连续失败 >= threshold--> OPEN
        OPEN --冷却 cooldown_s 到期--> HALF_OPEN
        HALF_OPEN --连续成功 >= probes--> CLOSED
        HALF_OPEN --任一失败--> OPEN
    """

    def __init__(self, key: str, policy: HealthPolicy, clock: Clock | None = None) -> None:
        self.key = key
        self._policy = policy
        self._clock: Clock = clock or SystemClock()

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_successes = 0
        self._probes_in_flight = 0

    # ---------------------------------------------------------------- 读
    @property
    def state(self) -> CircuitState:
        """当前状态。**惰性推进**：冷却到期时把 OPEN 视作 HALF_OPEN。

        这个「读时推进」是刻意的：若不做，就必须有一个后台任务定期扫所有熔断器 ——
        而那个任务的唯一作用就是改一个状态位，不值得引入一个常驻协程。
        """
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self._clock.monotonic() - self._opened_at >= self._policy.cooldown_s
        ):
            return CircuitState.HALF_OPEN
        return self._state

    # ---------------------------------------------------------------- 决策
    def allow(self) -> bool:
        """是否放行本次调用。

        **会改变状态**：OPEN 冷却到期后，第一次 ``allow()`` 把它推进到 HALF_OPEN。
        """
        current = self.state
        if current is not self._state:
            self._enter_half_open()

        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            return False

        # HALF_OPEN：限制并发探测数，避免冷却期一过就把全量流量放进去
        if self._probes_in_flight >= self._policy.half_open_probes:
            return False
        self._probes_in_flight += 1
        return True

    def release(self) -> None:
        """归还一个探测位，**既不记成功也不记失败**。

        用于「已放行、但最终没发起调用」的路径：被限流跳过、被取消、
        或在放行与调用之间预算耗尽。

        不归还的后果很具体：``HALF_OPEN`` 的探测位（``half_open_probes``，默认 2）
        会被永久占用，模型**再也恢复不到 CLOSED** —— 表现是「熔断后一直半开」，
        而日志里只有一条「进入半开探测」，看不出探测位泄漏。
        """
        self._probes_in_flight = max(0, self._probes_in_flight - 1)

    def record_success(self) -> None:
        self._probes_in_flight = max(0, self._probes_in_flight - 1)

        if self._state is CircuitState.HALF_OPEN:
            self._probe_successes += 1
            if self._probe_successes >= self._policy.half_open_probes:
                self._close()
            return

        # CLOSED 下的成功只清失败计数 —— 计数是「连续」失败，不是累计失败
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._probes_in_flight = max(0, self._probes_in_flight - 1)

        if self._state is CircuitState.HALF_OPEN:
            # 「任一失败」即回熔断 —— 见模块 docstring
            self._open()
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._policy.failure_threshold:
            self._open()

    # ---------------------------------------------------------------- 转换
    def _open(self) -> None:
        if self._state is not CircuitState.OPEN:
            _log.warning(
                "熔断打开：model=%s（连续失败 %d 次，冷却 %.0fs）",
                self.key, self._consecutive_failures, self._policy.cooldown_s,
            )
        self._state = CircuitState.OPEN
        self._opened_at = self._clock.monotonic()
        self._probe_successes = 0

    def _enter_half_open(self) -> None:
        self._state = CircuitState.HALF_OPEN
        self._probe_successes = 0
        self._probes_in_flight = 0
        _log.info("熔断冷却到期，进入半开探测：model=%s", self.key)

    def _close(self) -> None:
        _log.info("熔断恢复：model=%s", self.key)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._probe_successes = 0

    # ---------------------------------------------------------------- 观测
    def snapshot(self) -> dict[str, Any]:
        """给健康页/排障用。``state`` 取的是推进后的状态。"""
        return {
            "model": self.key,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "probe_successes": self._probe_successes,
        }

    def __repr__(self) -> str:
        return f"CircuitBreaker({self.key!r}, {self.state.value})"


class HealthRegistry:
    """按模型键管理熔断器。

    **一期不做跨进程共享**（架构概要设计-gateway §8 D-E）：
    多进程部署时各进程各自熔断，最坏情况是多几倍探测请求。
    跨进程共享的写竞争代价可能大于收益，放二期。
    """

    def __init__(self, policy: HealthPolicy, clock: Clock | None = None) -> None:
        self._policy = policy
        self._clock: Clock = clock or SystemClock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, key: str) -> CircuitBreaker:
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key, self._policy, self._clock)
            self._breakers[key] = breaker
        return breaker

    def allow(self, key: str) -> bool:
        return self.breaker(key).allow()

    def record_success(self, key: str) -> None:
        self.breaker(key).record_success()

    def record_failure(self, key: str) -> None:
        self.breaker(key).record_failure()

    def release(self, key: str) -> None:
        """归还探测位。见 :meth:`CircuitBreaker.release`。"""
        self.breaker(key).release()

    def snapshot(self) -> list[dict[str, Any]]:
        return [breaker.snapshot() for breaker in self._breakers.values()]

"""可注入的时钟：让「退避、冷却、限流窗口」都能被假时钟测试。

**为什么需要这个模块**：`src/gateway` 有三处逻辑**完全**依赖时间 ——
重试退避、熔断冷却、限流滑动窗口。若它们直接调 ``time.monotonic()`` 与
``asyncio.sleep()``，测试一个 30 秒的冷却期就要**真等 30 秒**。
结果是这些路径要么不被测，要么被写成 ``asyncio.sleep(0)`` 的假测试 —— 等于没测
（需求说明书-gateway NFR-G-07）。

**做法**：把「现在几点」和「睡多久」变成可注入的依赖。

**两个时间必须分开（最容易埋雷的一处）**：

=================  ==========================  ================================
方法                语义                        用途
=================  ==========================  ================================
``now()``          墙钟（epoch 秒）              落库时间戳、成本归属、日志
``monotonic()``    单调递增，不受系统调时影响     **deadline / 退避 / 冷却 / 窗口**
=================  ==========================  ================================

用墙钟算 deadline，会在 NTP 校时或用户改系统时间时**凭空超时或永不过期**。
``CallBudget`` 用绝对时刻表达 deadline，那个时刻**必须**取自 ``monotonic()``。

**FakeClock 放在 src 而不是 tests**：与 ``src/provider/mock/`` 同理 ——
可注入性本身是**契约的一部分**，不是某个测试文件的私有工具。
放在 tests 里的后果是每个测试各写一个，然后行为渐渐不一致。
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FakeClock"]


@runtime_checkable
class Clock(Protocol):
    """时间来源。``gateway`` / ``provider`` 只通过本协议取时间，不直接 import ``time``。"""

    def now(self) -> float:
        """墙钟：epoch 秒。用于落库时间戳，**不用于**计算时长。"""
        ...

    def monotonic(self) -> float:
        """单调时钟：只用于计算时长（deadline / 退避 / 冷却 / 窗口）。"""
        ...

    async def sleep(self, seconds: float) -> None:
        """异步等待。``seconds <= 0`` 时必须立即返回。"""
        ...


class SystemClock:
    """生产实现。无状态，可直接共享单例。"""

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        # asyncio.sleep(0) 是「让出一次事件循环」而非等待，语义不同，这里显式区分开，
        # 避免调用方以为自己在等而实际只是 yield。
        if seconds > 0:
            await asyncio.sleep(seconds)

    def __repr__(self) -> str:
        return "SystemClock()"


class FakeClock:
    """测试实现：时间只在 ``advance()`` / ``sleep()`` 时前进，从不真等待。

    **``sleep()`` 会推进虚拟时间，而不是立即返回** —— 这一点是刻意的。
    若 ``sleep()`` 立即返回，「退避 1s 后重试」在假时钟下就变成「退避 0s 后重试」，
    于是**退避与 deadline 的交互测不出来** —— 而那正是 ``FR-G-12`` 要保证的东西
    （总预算耗尽时必须停止，不得再发起尝试）。

    ``sleeps`` 记录每次等待的时长，供断言退避曲线：

        clock = FakeClock()
        ... 触发三次重试 ...
        assert clock.sleeps == [1.0, 2.0]      # 指数退避且无第三次
    """

    def __init__(self, *, start: float = 0.0) -> None:
        self._now = float(start)
        self._monotonic = float(start)
        self.sleeps: list[float] = []

    # ---------------------------------------------------------------- 读
    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    @property
    def total_slept(self) -> float:
        """累计等待时长 —— 断言「总耗时 ≤ deadline」时直接用它。"""
        return sum(self.sleeps)

    # ---------------------------------------------------------------- 写
    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"sleep 时长不能为负：{seconds}")
        # 只记录 > 0 的等待：0 是「让出事件循环」，不是时间流逝，记进去会污染退避断言。
        if seconds > 0:
            self.sleeps.append(float(seconds))
            self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """手动推进时间（用于模拟「冷却期到了」而不必真的等待）。"""
        if seconds < 0:
            raise ValueError(f"advance 时长不能为负：{seconds}")
        self._now += seconds
        self._monotonic += seconds

    def __repr__(self) -> str:
        return f"FakeClock(now={self._now}, slept={self.total_slept})"

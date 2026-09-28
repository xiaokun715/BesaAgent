"""装配：配置 → 注册表 → 网关。

**这是全仓库唯一一处「把所有东西接起来」的代码**（见 ``src/composition/__init__.py``）。
三个 app（cli / mcp / server）都调 :func:`build_runtime`，不各写一份。

**装配顺序是有约束的**：

1. 读配置（``foundation.settings``）；
2. 建注册表 —— 它会**在启动期做完全部校验**：厂商名拼错、候选引用悬空、
   策略名未知都会在这里硬失败；缺密钥只让该模型不可用（软失败，``NFR-G-05``）；
3. 建网关 —— 把重试/降级/限流/熔断/计价接上。

第 2 步排在第 3 步之前是刻意的：**配置错误必须在进程启动时就暴露**，
而不是等第一次线上调用。一个拼错的 alias 应该在 ``python -m apps.cli``
启动的瞬间就报出来。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from composition.event_sink import BufferingEmitter, flush_events
from composition.usage_sink import to_usage_rows, try_flush_usage
from foundation.clock import Clock, SystemClock
from foundation.database import Database
from foundation.settings import Settings, load_config
from gateway.cost import CostSheet
from gateway.fallback import FallbackPolicy
from gateway.gateway import EventEmitter, Gateway, NullEmitter
from gateway.health import HealthPolicy, HealthRegistry
from gateway.rate_limit import LocalRateLimiter, RateLimitPolicy, build_rate_limiter
from gateway.registry import Registry
from gateway.retry import RetryPolicy
from gateway.usage import UsageLedger
from repo.usage import UsageRepo, UsageRow

__all__ = ["Runtime", "build_runtime"]

_log = logging.getLogger(__name__)


@dataclass
class Runtime:
    """一次装配的产物。**用 ``async with`` 或显式 :meth:`aclose` 释放。**

    持有顺序即释放顺序的反向 —— ``gateway`` 在 ``registry`` 之上，
    而网关释放时会关掉注册表里的连接。
    """

    settings: Settings
    registry: Registry
    gateway: Gateway
    #: 持久化执行入口。**由 app 层（``apps/*/storage``）建好后注入** ——
    #: ``src/`` 不能 import ``apps/``，所以组合根只声明它、不构造它。
    #: ``None`` 表示这个进程不落库（如默认配置下的 CLI）。
    database: Database | None = None
    #: 事件缓冲器。``events`` 是注入的 ``EventEmitter`` 时它是 ``None``
    #: （那种情况下事件的去向由注入方决定，不归组合根管）。
    event_sink: BufferingEmitter | None = None

    async def flush_pending(self) -> None:
        """把待落库的**用量与事件在同一个事务里**交付。

        **为什么必须同一个事务**（``foundation/database.py`` 的设计目标）：

        > checkpoint 落库 + 事件落库 + 用量落库三步，若各是一个事务，
        > 「执行到一半进程挂了」会留下互相矛盾的记录，
        > 而断点续跑依赖的正是这三者的一致性。

        「事件说降级了、用量里没有对应记录」这种矛盾，事后谁都说不清是哪边丢了。

        **必须在 ``aclose()`` 之前调用**，而 ``aclose()`` 自己也会兜一次底。
        """
        if self.database is None:
            # 没有数据库（CLI 默认）：仍然要取走，避免缓冲区涨到上限后静默丢弃。
            await try_flush_usage(None, self.gateway.ledger)
            if self.event_sink is not None:
                self.event_sink.drain()
                self.event_sink.drain_dropped()
            return

        rows, dropped = self._take_usage()
        async with self.database.transaction() as tx:
            await UsageRepo(tx).record_ledger(rows, dropped=dropped)
            if self.event_sink is not None:
                await flush_events(tx, self.event_sink)

    def _take_usage(self) -> tuple[list[UsageRow], int]:
        """取走账本里的记录与丢弃数，转成行。

        **与事务分开是为了让两次 drain 紧挨着、中间没有 await** ——
        否则期间新产生的记录会被下一批交付，虽然不丢，但会让「这一批里有什么」
        变得不确定。取完之后再进事务，语义就干净了。
        """
        records = self.gateway.ledger.drain()
        dropped = self.gateway.ledger.drain_dropped()
        rows = to_usage_rows(records, occurred_at=datetime.now(timezone.utc))
        return rows, dropped

    async def aclose(self) -> None:
        """释放全部连接。**幂等**。

        **顺序是有约束的**：先 flush，再关连接。

        ``Gateway.aclose()`` 只关 provider 的 HTTP 客户端，**不 drain 它的
        ``UsageLedger``** —— 也就是说，不在这里补这一步的话，
        缓冲区里的用量与事件会跟着进程一起消失，而且**不会有任何错误**。

        而 flush 又必须排在关连接之前：要落库的数据得先进事务，
        连接关了就没地方写了。
        """
        await self.flush_pending()
        await self.gateway.aclose()

    async def __aenter__(self) -> Runtime:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()


def build_runtime(
    settings: Settings | None = None,
    *,
    env: str | None = None,
    config_dir: str | Path | None = None,
    env_vars: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    events: EventEmitter | None = None,
    ledger: UsageLedger | None = None,
    provider_options: Mapping[str, Any] | None = None,
    database: Database | None = None,
) -> Runtime:
    """按配置装配出可用的运行时。

    Args:
        settings: 已加载的配置。``None`` 时用 :func:`load_config` 从 ``configs/`` 读。
        provider_options: 原样转交给每个 provider 构造器 ——
            **测试注入 ``httpx.MockTransport`` 的入口**（``FR-P-13``）。
            生产路径不传，因此走真实网络。
        database: 持久化执行入口。**由 app 层建好后注入** ——
            ``src/`` 不能 import ``apps/``，所以这里只接收、不构造。
            ``None`` 表示这个进程不落库（默认配置下的 CLI 就是这种情形），
            此时用量记在内存里、随进程消失，与今天的 CLI 行为一致。

    Raises:
        SettingsError: 配置文件缺失 / 语法错误 / 插值变量未定义。
        ValueError: 配置自洽性校验失败（厂商名拼错、候选引用悬空、策略名未知）。
    """
    resolved_clock: Clock = clock or SystemClock()
    cfg = settings or load_config(env, config_dir=config_dir, env_vars=env_vars)

    gateway_cfg = cfg.gateway

    registry = Registry.from_config(
        gateway_cfg,
        models_cfg=cfg.models,
        providers_cfg=cfg.providers,
        env=cfg.env_vars or None,
        clock=resolved_clock,
        provider_options=provider_options,
    )

    # 没注入 events、又要落库时，用一个缓冲器把事件攒起来，由 flush_pending 交付。
    # **不落库时保持 NullEmitter**：攒起来也没地方去，只会白占内存 ——
    # 而「攒着不写」比「当场丢掉」更糟，因为它把丢失推迟到了缓冲区溢出那一刻。
    event_sink = BufferingEmitter() if (events is None and database is not None) else None

    gateway = Gateway(
        registry,
        retry=RetryPolicy.from_config(gateway_cfg.get("retry")),
        fallback=FallbackPolicy.from_config(gateway_cfg.get("fallback")),
        health=HealthRegistry(
            HealthPolicy.from_config(gateway_cfg.get("health")), resolved_clock
        ),
        rate_limit=_build_limiter(gateway_cfg.get("rate_limit"), resolved_clock),
        ledger=ledger or UsageLedger(),
        cost=CostSheet.from_config(gateway_cfg.get("cost")),
        events=events or event_sink or NullEmitter(),
        clock=resolved_clock,
        deadline_s=_deadline(gateway_cfg),
    )

    _log.info(
        "运行时装配完成：env=%s，模型 %d 个，逻辑名 %s，落库=%s",
        cfg.env,
        len(registry.models),
        ", ".join(sorted(registry.aliases)) or "（无）",
        "开（用量 + 事件）" if database is not None else "关（只留在内存）",
    )
    return Runtime(
        settings=cfg,
        registry=registry,
        gateway=gateway,
        database=database,
        event_sink=event_sink,
    )


def _build_limiter(cfg: Mapping[str, Any] | None, clock: Clock) -> LocalRateLimiter:
    """构造限流器。

    ``enabled: false`` 时返回一个**空策略**的限流器（三个维度都不限制），
    而不是 ``None`` —— 让网关侧不需要到处判空，也让「关掉限流」与
    「没配限流」走同一条代码路径。
    """
    data = dict(cfg or {})

    if not data.get("enabled", True):
        # 注意：这里**不走 build_rate_limiter**，因为后者会校验 backend。
        # 「关掉限流」应当无条件成立，哪怕 backend 配的是尚未实现的 redis ——
        # 否则关掉它反而会报错，与本意相反。
        return LocalRateLimiter(RateLimitPolicy(), clock=clock, on_exceed="fallback")

    return build_rate_limiter(data, clock=clock)


def _deadline(gateway_cfg: Mapping[str, Any]) -> float | None:
    section = gateway_cfg.get("deadline")
    if isinstance(section, Mapping) and section.get("default_s") is not None:
        return float(section["default_s"])
    return 120.0

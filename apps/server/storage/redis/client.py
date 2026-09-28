"""Redis 客户端的构造。

**与 ``postgres/engine.py`` 的分工完全相同**：``src/`` 只定义协议
（``IdempotencyStore``），**怎么连**在 app 层。
组合根拿到实现后再注入给它需要的地方。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from apps.server.storage.redis.idempotency import RedisIdempotencyStore

__all__ = ["build_client", "build_idempotency_store", "ping"]

_log = logging.getLogger(__name__)


def build_client(cfg: Mapping[str, Any], **options: Any) -> Any:
    """按 ``redis:`` 配置段建异步客户端。

    **用连接池而不是每次新建** —— 与 provider 的 HTTP 客户端复用同一条理由：
    每次新建连接会让每个请求都付一次握手成本，而在多 agent 并发下会打爆 fd。

    Raises:
        ValueError: ``url`` 缺失。
        ImportError: 没装 ``redis`` 可选依赖。
    """
    url = str(cfg.get("url") or "").strip()
    if not url:
        raise ValueError("redis.url 为空；请在 .env 里设 BESA_REDIS_URL，或检查 configs/*.yaml")

    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - 可选依赖
        raise ImportError(
            "缺少 redis 依赖。装法：pip install 'besa-agent[redis]'"
        ) from exc

    return Redis.from_url(url, decode_responses=False, **options)


def build_idempotency_store(
    cfg: Mapping[str, Any],
    *,
    prefix: str = RedisIdempotencyStore.DEFAULT_PREFIX,
    client: Any | None = None,
) -> RedisIdempotencyStore:
    """建幂等存储。

    ``prefix`` 来自 ``tool.idempotency.key_prefix``。
    ⚠ **不要用 ``besa:``** —— 本机 6379 上那个命名空间已被兄弟项目的 Celery 占用。
    """
    return RedisIdempotencyStore(client or build_client(cfg), prefix=prefix)


async def ping(store: RedisIdempotencyStore) -> bool:
    """探活，并把结果写进 ``store.available``。供启动期与 ``doctor`` 用。"""
    ok = await store.ping()
    if not ok:
        _log.warning(
            "Redis 不可达。**不会静默降级**：有副作用的工具将被拒绝执行，"
            "只读工具会放行但每次都告警（见 tool.idempotency.on_redis_unavailable）。"
        )
    return ok

"""Redis 后端：客户端构造，以及**必须绑定具体后端**的实现。

**本包与 ``postgres/`` 的分工一致**（``docs/server_storage`` 的 `NFR-S-01`）:
``src/`` 定义协议，这里实现。目前只有一个实现 —— 幂等的快路径。

⚠ **本机这个 Redis 不是我们独占的**：实测 6379 上已有
``besa:queue:ingest`` 与 ``_kombu.binding.*``（一个 Celery 部署）。
所以：

1. 键前缀统一 ``besa_agent:``，**不要用 ``besa:``**；
2. **严禁 ``FLUSHDB`` / ``FLUSHALL``**，生产禁用 ``KEYS *`` —— 会清掉别人的队列；
3. 我们自己的键必须有 TTL —— 该实例实测 ``maxmemory = 0``（无上限、无淘汰兜底），
   无 TTL 的键会一直涨到把机器吃光。
"""

from __future__ import annotations

from apps.server.storage.redis.client import (
    build_client,
    build_idempotency_store,
    ping,
)
from apps.server.storage.redis.idempotency import RedisIdempotencyStore

__all__ = [
    "RedisIdempotencyStore",
    "build_client",
    "build_idempotency_store",
    "ping",
]

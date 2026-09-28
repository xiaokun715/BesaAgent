"""Redis 侧的幂等快路径（``IdempotencyStore`` 的实现）。

**两个键，不是一个**（架构文档 ``T-C``）—— 它们的语义与生命周期不同：

============  ==========================  ============================
``claim:``     「有人正在做」的**短期租约**  短（``lease_s``）；丢了最多是并发去做，
                                             而库里的 ``in_flight`` 会兜住
``done:``      「已经做完了」的**结果缓存**  长（``result_ttl_s``）；丢了回库查
============  ==========================  ============================

**抢占必须是一个原子判定**：判断已完成 / 抢占 / 已被占三者不能分开做，
中间任何一步被插进来，都会让**已完成的键被重新执行**。
所以它是一个 Lua 脚本 —— 与 ``docs/server_storage`` 的 ``DS-4`` 同一条理由：
「读-判断-写」必须原子。

⚠ **键前缀是 ``besa_agent:``** —— 本机 6379 上 ``besa:`` 已被兄弟项目的
Celery 队列占用（``besa:queue:ingest`` / ``_kombu.binding.*``）。
**绝不能对这个实例执行 ``FLUSHDB``**，那会清掉别人的队列。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from tool.idempotency import ClaimState, StoreUnavailable

__all__ = ["REDIS_ERRORS", "RedisIdempotencyStore"]

_log = logging.getLogger(__name__)

#: 连接类异常。**在导入期解析**，避免每次调用都去问 redis 的异常层次。
try:  # pragma: no cover - 依赖已在 pyproject 的可选组里
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import RedisError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    REDIS_ERRORS: tuple[type[BaseException], ...] = (
        RedisError,
        RedisConnectionError,
        RedisTimeoutError,
        OSError,
    )
except ImportError:  # pragma: no cover
    REDIS_ERRORS = (OSError,)


#: 原子抢占。
#:
#: 返回值恰好对应上层要做的三件事：**直接复用 / 我来做 / 等或拒**。
_CLAIM_LUA = """
-- KEYS[1]=claim  KEYS[2]=done   ARGV[1]=owner  ARGV[2]=lease_ms
local payload = redis.call('GET', KEYS[2])
if payload then
  return {'done', payload}
end
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
  return {'claimed', ''}
end
return {'in_flight', ''}
"""

#: 释放：**校验持有者再删**。
#:
#: 不校验的话会出现这样一串（每一步都不报错）：
#:
#:     A 拿到租约 → A 卡住超过租约 → 租约过期 → B 拿到 → A 醒来 DEL → 删掉了 B 的
#:     → C 也拿到 → B 与 C 同时执行
#:
#: 与 ``docs/server_storage`` 的 ``S-G``（分布式锁的释放）是同一条纪律。
_RELEASE_LUA = """
-- KEYS[1]=claim  ARGV[1]=owner
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisIdempotencyStore:
    """基于 Redis 的实现。

    **``available`` 是「最近一次已知状态」，不是实时探测** —— 每次调用都 ping
    会让快路径多一次往返，而它存在的意义正是省掉往返。
    真正的探测交给 :meth:`ping`（启动时与健康检查调用）。
    """

    #: Redis 键前缀。见模块 docstring —— ``besa:`` 不属于我们。
    DEFAULT_PREFIX: ClassVar[str] = "besa_agent:tool:"

    __slots__ = ("_client", "_prefix", "_available", "_claim_script", "_release_script")

    def __init__(
        self,
        client: Any,
        *,
        prefix: str = DEFAULT_PREFIX,
        assume_available: bool = True,
    ) -> None:
        self._client = client
        self._prefix = prefix.rstrip(":") + ":"
        self._available = bool(assume_available)
        self._claim_script = client.register_script(_CLAIM_LUA)
        self._release_script = client.register_script(_RELEASE_LUA)

    # ---------------------------------------------------------------- 探活
    @property
    def available(self) -> bool:
        return self._available

    async def ping(self) -> bool:
        """主动探活并更新 :attr:`available`。"""
        try:
            await self._client.ping()
            self._available = True
        except REDIS_ERRORS as exc:  # pragma: no cover - 需要真的断开
            _log.warning("Redis 不可用（幂等将按副作用分级降级）：%s", exc)
            self._available = False
        return self._available

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- 键
    def _claim_key(self, key: str) -> str:
        return f"{self._prefix}claim:{key}"

    def _done_key(self, key: str) -> str:
        return f"{self._prefix}done:{key}"

    # ---------------------------------------------------------------- 契约
    async def claim(self, key: str, owner: str, *, lease_s: float) -> ClaimState:
        if not self._available:
            raise StoreUnavailable("Redis 不可用")

        lease_ms = max(1, int(lease_s * 1000))
        try:
            raw = await self._claim_script(
                keys=[self._claim_key(key), self._done_key(key)],
                args=[owner, lease_ms],
            )
        except REDIS_ERRORS as exc:
            # **中途断开**：标记不可用并抛出 —— 由 guard 按副作用分流。
            # 不要在这里返回一个「看起来正常」的状态，那会把一次降级
            # 伪装成一次正常的幂等判定。
            self._available = False
            raise StoreUnavailable(f"Redis 调用失败：{type(exc).__name__}: {exc}") from exc

        state = _decode(raw[0]) if raw else "in_flight"
        payload = _decode(raw[1]) if raw and len(raw) > 1 else ""
        if state == "done":
            return ClaimState("done", payload or None)
        if state == "claimed":
            return ClaimState("claimed")
        return ClaimState("in_flight")

    async def mark_done(self, key: str, payload: str, *, ttl_s: float) -> None:
        if not self._available:
            return
        try:
            await self._client.set(
                self._done_key(key), payload, ex=max(1, int(ttl_s))
            )
        except REDIS_ERRORS as exc:  # pragma: no cover - 需要真的断开
            # 结果缓存写不进去**不是致命**的：权威记录里已经有 done 了，
            # 下一次会从库里查到。所以这里只标记不可用，不抛。
            self._available = False
            _log.warning("结果缓存写入失败（不影响正确性，权威记录仍在库里）：%s", exc)

    async def release(self, key: str, owner: str) -> None:
        if not self._available:
            return
        try:
            await self._release_script(keys=[self._claim_key(key)], args=[owner])
        except REDIS_ERRORS:  # pragma: no cover - 需要真的断开
            self._available = False


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")

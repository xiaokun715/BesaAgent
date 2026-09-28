"""幂等协议 —— 本模块的核心。

**两阶段，不是一次抢占**。只做一次 ``SET NX`` 的话会出现这样一串，
而其中每一步都不报错：

    A 抢占成功 → A 执行（副作用已发生）→ A 崩溃，没来得及记录
                                              ↓
    B 提交同一个键 → 没有 done 记录 → 认为没做过 → 再执行一遍
                                              ↓
                            副作用发生了两次，而系统认为只发生了一次

所以是 **登记（先于执行）→ 执行 → 登记完成**，把「正在执行」显式化。

**Redis 是快路径，PostgreSQL 是权威**（``DT-3``）。纯 Redis 的问题不是「会丢」，
而是「**一次清库就静默地变成可以重复执行**」—— 那正是本模块存在的理由。

**这个类对外只有三个方法**，且顺序由 ``executor`` 保证：
``claim`` → （执行）→ ``mark_done`` / ``mark_failed``。
把它做成一个「块」而不是散落的几次读写，是为了让顺序**只有一个地方能写错**。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from foundation.database import Database
from repo.tool import ExecutionView, ToolExecutionRepo
from tool.types import Outcome, SideEffect, ToolInvocation, ToolResult

__all__ = [
    "Claim",
    "ClaimState",
    "IdempotencyGuard",
    "IdempotencyStore",
    "StoreUnavailable",
    "UnavailableStore",
    "args_digest",
    "derive_key",
]

_log = logging.getLogger(__name__)

ClaimStateName = Literal["claimed", "in_flight", "done"]

#: 幂等不可用时，各副作用等级的默认行为。
#:
#: **只读放行、写入拒绝**（`DT-2`）：只读重复的代价是「浪费一次 IO」，
#: 为了它让整个 agent 停工代价不成比例；而写入重复会产生第二份内容，
#: 不可逆的操作更不必说。
DEFAULT_ON_UNAVAILABLE: Mapping[str, str] = {
    "read": "allow_with_warning",
    "write": "refuse",
    "destructive": "refuse",
}


# --------------------------------------------------------------------------- #
# 键
# --------------------------------------------------------------------------- #


def args_digest(invocation: ToolInvocation) -> str:
    """**参数指纹**：只做 JSON 的确定性序列化。

    规范化**仅限**「跨重试必然一致」的部分：键排序、无多余空白、非 ASCII 不转义。
    **不对参数值做任何语义加工** —— 不 strip、不做路径归一化、不折叠大小写。

    理由是一条非对称性：

        **去重失败是安全的（多跑一次），误去重是危险的（少跑一次，且没人知道）。**

    所以 ``./a.txt`` 与 ``a.txt`` 算**不同次**（归一化路径需要「相对谁」的上下文，
    而那个上下文可能变），``"content":"x "`` 与 ``"content":"x"`` 也算**不同次**
    （``write`` 的内容里有意义）。数值等价同样不归一化 —— 它要求「所有参数值的
    类型语义都被正确理解」，而参数是任意 JSON。
    """
    raw = json.dumps(
        dict(invocation.arguments), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(f"{invocation.tool_name}\x00{raw}".encode()).hexdigest()


def _encode_payload(output: str, truncated: bool) -> str:
    """把结果编成缓存载荷。

    **为什么不是直接存文本**：空载荷分不清两件事 ——
    「结果本来就是空的」与「结果太大没能缓存」。而这两者对调用方的意义完全不同：
    后者要告诉它「去拿真正的结果，别以为这里就是全部」。
    用一个信封把 ``truncated`` 一起存下来，这个区分才在 Redis 的快路径上成立
    （走数据库那条路本来就有 ``result_truncated`` 列）。
    """
    return json.dumps({"o": output, "t": truncated}, ensure_ascii=False)


def _decode_payload(raw: str | None) -> tuple[str, bool]:
    if not raw:
        return "", False
    try:
        data = json.loads(raw)
    except ValueError:
        # 容错：手工写进去的、或将来换了格式的值，当作纯文本结果
        return raw, False
    if not isinstance(data, dict):
        return raw, False
    return str(data.get("o") or ""), bool(data.get("t"))


def derive_key(invocation: ToolInvocation) -> str:
    """派生幂等键：**作用域 + 参数指纹**。

    **显式给的键优先**（``DT-1``）—— 那种情况在 :meth:`IdempotencyGuard.claim`
    里已经短路，走不到这里。

    作用域必须进键：同一份参数在**不同的会话/运行**里是不同的调用。
    少了它，两个会话里各读一次同一个文件会被当成同一次，
    第二个会话拿到的是第一次的内容 —— 而文件可能已经变了。
    """
    scope = invocation.scope or "-"
    return f"{scope}:{args_digest(invocation)}"


# --------------------------------------------------------------------------- #
# 存储契约
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClaimState:
    """``claim`` 的返回值：**判断已完成 / 抢占 / 已被占**三件事一次问完。

    三者在 Redis 侧是一个 Lua 脚本（读-判断-写必须原子），
    所以合成一个返回值而不是三个方法 —— 分开调用会在中间留出窗口，
    而那个窗口恰好会让**已完成的键被重新执行**。
    """

    state: ClaimStateName
    #: ``state == "done"`` 时是上次的结果载荷
    payload: str | None = None

    @property
    def claimed(self) -> bool:
        return self.state == "claimed"


class IdempotencyStore(Protocol):
    """Redis 的快路径。**由 ``apps/server/storage/redis`` 实现**。

    **窄接口而不是直接给客户端**（架构文档 §6）：

    1. 单测可以用内存实现，不必起 Redis；
    2. Lua 的原子性边界收在一处 —— 散出去的话，「读-判断-写」会在多处被拆开
       而没人发现；
    3. 与 ``Database`` 的注入方式一致，组合根的装配代码形状统一。
    """

    @property
    def available(self) -> bool:
        """Redis 现在可用吗。**每次调用都要重新看** —— 它是会被翻转的。"""
        ...

    async def claim(self, key: str, owner: str, *, lease_s: float) -> ClaimState:
        """尝试占用这个键。"""
        ...

    async def mark_done(self, key: str, payload: str, *, ttl_s: float) -> None:
        """标记完成并缓存结果。"""
        ...

    async def release(self, key: str, owner: str) -> None:
        """释放占用。**必须校验持有者** —— 不校验的话，
        「自己超时 → 别人拿到 → 自己醒来删掉别人的锁」这条链会让去重失效。"""
        ...


class StoreUnavailable(RuntimeError):
    """存储**中途**变得不可用（调用过程中连接断了）。

    与「启动时就没有 Redis」（``available`` 为假）是两件事：
    后者是配置事实，前者是运行时故障。但**处置相同** ——
    按副作用等级分流，而不是把工具调用报成「执行失败」。
    报成失败会让上层去重试，而重试恰恰是需要幂等保护的动作。
    """


class UnavailableStore:
    """永远不可用的存储。

    给「这个部署没有 Redis」用（CLI 默认就是）。于是所有写类工具
    **自然被拒**，行为与配置一致，而代码里不需要额外的 ``if redis is None`` 分支 ——
    那种分支最容易在新增一条路径时被漏掉。
    """

    __slots__ = ()

    @property
    def available(self) -> bool:
        return False

    async def claim(self, key: str, owner: str, *, lease_s: float) -> ClaimState:
        return ClaimState("in_flight")  # pragma: no cover - 不会走到，_unavailable 先短路

    async def mark_done(self, key: str, payload: str, *, ttl_s: float) -> None:
        return None

    async def release(self, key: str, owner: str) -> None:
        return None


# --------------------------------------------------------------------------- #
# 判决
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Claim:
    """一次幂等申请的结果。

    ``can_execute`` 为真时 ``outcome`` 是**预兆**（``executed``）而不是结论 ——
    真正的结论在执行之后由 ``mark_done`` / ``mark_failed`` 落定。
    """

    can_execute: bool
    outcome: Outcome
    idem_key: str
    owner: str
    tool_name: str
    side_effect: str
    scope: str
    reused_output: str = ""
    reused_truncated: bool = False
    reason: str = ""

    @property
    def degraded(self) -> bool:
        """这次是在**没有幂等保护**的情况下放行的吗。"""
        return self.outcome == "executed_degraded"

    def as_result(self) -> ToolResult:
        """把这个判决变成给调用方的结果（不执行时用）。"""
        if self.outcome == "reused":
            return ToolResult(
                outcome="reused",
                tool_name=self.tool_name,
                output=self.reused_output,
                truncated=self.reused_truncated,
                idem_key=self.idem_key,
            )
        return ToolResult(
            outcome=self.outcome,
            tool_name=self.tool_name,
            error=self.reason,
            idem_key=self.idem_key,
        )


# --------------------------------------------------------------------------- #
# 守卫
# --------------------------------------------------------------------------- #


class IdempotencyGuard:
    """把「一次幂等调用」包成一个块。

    **它是 ``IdempotencyStore`` 与 ``Database`` 的唯一汇合点** ——
    快路径与权威记录的顺序、回填、冲突处理都只在这里写一次。
    """

    __slots__ = ("_store", "_database", "_lease_s", "_result_ttl_s", "_max_cached_bytes", "_policy")

    def __init__(
        self,
        store: IdempotencyStore,
        database: Database | None = None,
        *,
        lease_s: float = 120.0,
        result_ttl_s: float = 3600.0,
        max_cached_result_bytes: int = 65_536,
        on_unavailable: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._database = database
        self._lease_s = float(lease_s)
        self._result_ttl_s = float(result_ttl_s)
        self._max_cached_bytes = int(max_cached_result_bytes)
        policy = dict(DEFAULT_ON_UNAVAILABLE)
        policy.update(on_unavailable or {})
        self._policy = policy

    # ---------------------------------------------------------------- 申请
    async def claim(self, invocation: ToolInvocation, effect: SideEffect) -> Claim:
        """申请执行权。

        返回的 :class:`Claim` 若 ``can_execute`` 为假，**调用方不得执行**，
        直接把 ``claim.as_result()`` 返回给上层。
        """
        key = invocation.idempotency_key or derive_key(invocation)
        owner = uuid.uuid4().hex

        base = {
            "idem_key": key,
            "owner": owner,
            "tool_name": invocation.tool_name,
            "side_effect": effect,
            "scope": invocation.scope,
        }

        # **没有权威记录才是真的没有幂等** —— 权威记录不在，Redis 只是缓存，
        # 清一次库就静默地变成可以重复执行。所以只有这一种情况要分流。
        if self._database is None:
            return self._unavailable(base, effect, missing="没有配置数据库（幂等需要权威记录）")

        # Redis 是**快路径**，不是必需品：它不在时退到「只用权威记录」——
        # 慢一点（每次多一次 SELECT + INSERT），但**语义完全正确**，
        # 因为唯一约束 (scope, idem_key) 本身就是正确的并发闸门。
        if self._store.available:
            try:
                state = await self._store.claim(key, owner, lease_s=self._lease_s)
            except StoreUnavailable as exc:
                _log.warning(
                    "Redis 中途不可用，退到只用权威记录的慢路径"
                    "（去重仍然生效，只是每次多一次库往返）：%s",
                    exc,
                )
                state = None

            if state is not None:
                if state.state == "done":
                    output, truncated = _decode_payload(state.payload)
                    return Claim(
                        can_execute=False,
                        outcome="reused",
                        reused_output=output,
                        reused_truncated=truncated,
                        reason=(
                            "结果超过缓存上限，内容未缓存（去重仍然生效）" if truncated else ""
                        ),
                        **base,
                    )
                if state.state == "in_flight":
                    # 有人正在做。**不执行**，也不等 —— 等多久是上层的决定
                    return Claim(
                        can_execute=False,
                        outcome="in_flight",
                        reason="同一个调用正在执行中（并发重复被挡住）",
                        **base,
                    )
                # claimed：Redis 说可以做。但**权威记录说了才算** —— Redis 可能被清过。
        else:
            _log.warning(
                "Redis 不可用，走只用权威记录的慢路径（去重仍然生效，"
                "但每次调用多一次库往返）。"
            )

        return await self._confirm_with_authority(invocation, base, effect)

    async def _confirm_with_authority(
        self, invocation: ToolInvocation, base: dict[str, Any], effect: SideEffect
    ) -> Claim:
        """去权威记录确认。**这是幂等的真正判定处** —— Redis 只是它的缓存。"""
        assert self._database is not None

        try:
            async with self._database.transaction() as tx:
                repo = ToolExecutionRepo(tx)
                existing: ExecutionView | None = await repo.find(
                    invocation.scope, base["idem_key"], lease_s=self._lease_s
                )

                if existing is not None and existing.done:
                    # Redis 说没做过（或 Redis 不在），库说做过 → **以库为准**
                    await self._backfill_cache(existing)
                    return Claim(
                        can_execute=False,
                        outcome="reused",
                        reused_output=existing.result or "",
                        reused_truncated=existing.result_truncated,
                        reason="权威记录显示已完成（Redis 无记录或缓存已过期）",
                        **base,
                    )

                if existing is not None and existing.state in {"in_flight", "uncertain"}:
                    # 库说有人在做（或状态不确定）→ 我们不该执行。
                    # **把可能拿到的 Redis 占用还回去**：留着只会白占一个租约，
                    # 而别人照样会被库挡住，所以还回去是安全的。
                    await self._store.release(base["idem_key"], base["owner"])
                    return Claim(
                        can_execute=False,
                        outcome="uncertain" if existing.uncertain else "in_flight",
                        reason=(
                            "权威记录显示上一次执行没有正常结束（崩溃窗口）—— "
                            "副作用到底发生没有**不知道**，重试是上层的决定"
                            if existing.uncertain
                            else "权威记录显示另一个执行者正在进行"
                        ),
                        **base,
                    )

                # 上次失败了 → **重开那条记录**，而不是再插一条。
                # 唯一约束会让「再插一条」失败，而那个失败的表现是「有人在跑」——
                # 于是这个键被永久锁死，且没有任何报错。
                if existing is not None and existing.retryable:
                    reopened = await repo.reopen(
                        scope=invocation.scope, idem_key=base["idem_key"], owner=base["owner"]
                    )
                    if reopened:
                        return Claim(can_execute=True, outcome="executed", **base)

                # 库里没有记录 → 登记
                registered = await repo.register(
                    scope=invocation.scope,
                    idem_key=base["idem_key"],
                    tool_name=invocation.tool_name,
                    side_effect=effect,
                    args_digest=args_digest(invocation),
                    owner=base["owner"],
                    trace_id=invocation.trace_id,
                    session_id=invocation.session_id,
                    caller=invocation.caller,
                )
        except StoreUnavailable:  # pragma: no cover - 存储层不抛这个
            raise
        except Exception as exc:  # noqa: BLE001
            # **权威记录不可用** = 真的没有幂等保护 → 按副作用分流。
            # 报成「执行失败」会诱导上层重试，而重试恰恰是最需要幂等保护的动作。
            _log.warning("权威记录不可用：%s", exc)
            return self._unavailable(base, effect, missing=f"数据库不可用（{type(exc).__name__}）")

        if not registered:
            # 唯一约束挡住了我们 —— 有人比我们先到。**不是错误**。
            await self._store.release(base["idem_key"], base["owner"])
            return Claim(
                can_execute=False,
                outcome="in_flight",
                reason="并发登记冲突：另一个执行者刚刚抢先登记",
                **base,
            )

        return Claim(can_execute=True, outcome="executed", **base)

    async def _backfill_cache(self, existing: ExecutionView) -> None:
        """把权威记录里的结果回填进 Redis 缓存。**失败不影响正确性。**"""
        if not self._store.available:
            return
        try:
            await self._store.mark_done(
                existing.idem_key,
                _encode_payload(existing.result or "", existing.result_truncated),
                ttl_s=self._result_ttl_s,
            )
        except Exception:  # noqa: BLE001 - 回填是优化，失败就下次再回填
            _log.debug("结果缓存回填失败", exc_info=True)

    # ---------------------------------------------------------------- 收尾
    async def mark_done(self, claim: Claim, result: ToolResult) -> None:
        """登记完成，并（在结果不太大时）缓存结果。

        **结果超过上限时不缓存内容，但去重仍然生效** ——
        复用时会告诉调用方「结果太大，没能缓存」，而不是返回一个被截断的结果。
        假装复用了，会让下游基于不完整信息做判断。

        **没有权威记录时（降级放行的只读调用）什么都不写** ——
        那种情况下本来就没有记录可更新，硬写会报「要更新一条不存在的记录」，
        而那条告警会把「有意降级」伪装成「有人绕过了 executor」。
        """
        encoded = result.output.encode("utf-8")
        cacheable = len(encoded) <= self._max_cached_bytes

        if self._database is None:
            return

        async with self._database.transaction() as tx:
            await ToolExecutionRepo(tx).mark_done(
                scope=claim.scope,
                idem_key=claim.idem_key,
                result=result.output if cacheable else None,
                truncated=result.truncated or not cacheable,
                output_bytes=len(encoded),
            )

        if not self._store.available:
            return
        # 无论能不能缓存内容，**都要标记 done**（去重必须生效）。
        # 区别只在载荷里 —— `truncated` 为真时，复用的调用方会被告知
        # 「内容没缓存」，而不是拿到一个空结果却以为那就是全部。
        await self._store.mark_done(
            claim.idem_key,
            _encode_payload(result.output if cacheable else "", result.truncated or not cacheable),
            ttl_s=self._result_ttl_s,
        )

    async def mark_failed(self, claim: Claim, error: str) -> None:
        """登记失败，并**释放占用** —— 失败是要能重试的。"""
        if self._database is not None:
            async with self._database.transaction() as tx:
                await ToolExecutionRepo(tx).mark_failed(
                    scope=claim.scope, idem_key=claim.idem_key, error=error
                )
        if self._store.available:
            await self._store.release(claim.idem_key, claim.owner)

    # ---------------------------------------------------------------- 降级
    def _unavailable(self, base: dict[str, Any], effect: SideEffect, *, missing: str) -> Claim:
        """幂等不可用时按副作用分流（`FR-T-06` / `DT-2`）。"""
        action = self._policy.get(effect, "refuse")

        if action == "allow_with_warning" and effect == "read":
            _log.warning(
                "幂等不可用（%s），但工具 %s 是只读的 —— 放行执行。"
                "**注意：这次执行没有幂等保护，重复提交会真的执行多次**"
                "（只读重复无害，所以这是刻意的取舍）。",
                missing,
                base["tool_name"],
            )
            return Claim(
                can_execute=True,
                # **独立档位**：不能与普通 executed 混同，
                # 否则「偶尔出现的重复执行」就永远查不出原因
                outcome="executed_degraded",
                reason=f"幂等不可用（{missing}）",
                **base,
            )

        return Claim(
            can_execute=False,
            outcome="refused",
            reason=(
                f"幂等不可用（{missing}），工具 {base['tool_name']} 的副作用等级是 "
                f"{effect!r} —— **拒绝执行**。\n"
                "重复执行有副作用的工具可能产生事故，宁可停手。\n"
                "要改这个行为，配置 tool.idempotency.on_redis_unavailable。"
            ),
            **base,
        )

"""幂等协议（``tool/idempotency.py``）—— 本模块的核心。

**为什么这组是全模块最该写厚的一组**：幂等失效的方式**全都不报错**。
重复执行了、把两次不同调用当成一次、崩溃后的状态被当成成功 ——
每一种都是「系统认为一切正常，而账单/副作用是错的」。

所以下面每条断言都冲着某个具体的静默失效去。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from repo.tool import ToolExecutionRepo
from tool.idempotency import IdempotencyGuard, UnavailableStore, args_digest, derive_key
from tool.types import ToolInvocation, ToolResult

from .conftest import EchoTool, InMemoryStore


def _inv(**over) -> ToolInvocation:
    base = {"tool_name": "echo", "arguments": {"text": "hi"}, "scope": "s-1"}
    base.update(over)
    return ToolInvocation(**base)


# --------------------------------------------------------------------------- #
# 键：显式优先 + 派生兜底
# --------------------------------------------------------------------------- #


def test_params_fingerprint_ignores_key_order():
    """键序是序列化噪声，必须被消掉 —— 否则模型换个参数顺序就是「新调用」。"""
    a = _inv(arguments={"x": 1, "y": 2})
    b = _inv(arguments={"y": 2, "x": 1})
    assert args_digest(a) == args_digest(b)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"path": "./a.txt"}, {"path": "a.txt"}),      # 路径不做归一化
        ({"content": "x "}, {"content": "x"}),          # 内容不 strip
        ({"n": 1}, {"n": 1.0}),                         # 数值不做等价归一化
    ],
)
def test_semantic_normalization_is_deliberately_not_done(left, right):
    """**刻意不做语义归一化。**

    这条条非对称性：**去重失败是安全的（多跑一次），误去重是危险的（少跑一次，
    且没人知道）**。而语义归一化要求「理解参数值的语义」，参数却是任意 JSON ——
    算错的方向恰好是危险的那一边。
    """
    assert args_digest(_inv(arguments=left)) != args_digest(_inv(arguments=right))


def test_scope_is_part_of_the_derived_key():
    """**不同作用域是不同的调用。**

    少了它，两个会话里各读一次同一个文件会被当成同一次，
    第二个会话拿到的是第一次的内容 —— 而文件可能已经变了。
    """
    assert derive_key(_inv(scope="s-1")) != derive_key(_inv(scope="s-2"))


def test_explicit_key_wins_over_derived():
    """显式给的键优先（`DT-1`）—— 它能表达「参数一样但确实要各跑一遍」。"""
    a = _inv(idempotency_key="explicit-1", arguments={"x": 1})
    b = _inv(idempotency_key="explicit-1", arguments={"x": 999})
    # 参数不同、显式键相同 → 走的是同一个键
    assert a.idempotency_key == b.idempotency_key


# --------------------------------------------------------------------------- #
# 两阶段：登记先于执行
# --------------------------------------------------------------------------- #


async def test_claim_registers_in_flight_before_execution(guard: IdempotencyGuard, db):
    """**登记必须先于执行。**

    反过来的话，崩在「执行完但没登记」之间，下次重试会**再执行一遍**，
    而系统完全不知道发生过。
    """
    claim = await guard.claim(_inv(), "read")
    assert claim.can_execute

    async with db.transaction() as tx:
        view = await ToolExecutionRepo(tx).find("s-1", claim.idem_key)
    assert view is not None and view.state == "in_flight", (
        "claim 返回时必须已经登记，否则崩溃窗口无法被发现"
    )


async def test_completed_key_is_reused(guard: IdempotencyGuard):
    """已在库里 done 的键 → 复用，**不再执行**。"""
    first = await guard.claim(_inv(), "read")
    await guard.mark_done(
        first, ToolResult(outcome="executed", tool_name="echo", output="结果")
    )

    second = await guard.claim(_inv(), "read")
    assert not second.can_execute
    assert second.outcome == "reused"
    assert second.reused_output == "结果"


async def test_in_flight_blocks_concurrent_duplicate(guard: IdempotencyGuard):
    """并发提交同一个键：第二个被挡住，**不得自己再执行一遍**。"""
    first = await guard.claim(_inv(), "read")
    assert first.can_execute

    second = await guard.claim(_inv(), "read")
    assert not second.can_execute
    assert second.outcome == "in_flight"


async def test_authoritative_record_survives_a_redis_flush(store, db):
    """**清空 Redis 之后仍然不能重复执行。**

    这是「纯 Redis 被否决」的全部理由：纯 Redis 下，一次清库就
    **静默地变成可以重复执行** —— 不是「查不到」，是「以为没做过」。
    """
    guard = IdempotencyGuard(store, db, lease_s=60)
    first = await guard.claim(_inv(), "write")
    await guard.mark_done(first, ToolResult(outcome="executed", tool_name="echo", output="已写"))

    store.flush_redis()  # Redis 被清空，权威记录不动

    again = await guard.claim(_inv(), "write")
    assert not again.can_execute, "清库后绝不能变成可重复执行"
    assert again.outcome == "reused"
    assert again.reused_output == "已写", "结果要能从权威记录里回来"


# --------------------------------------------------------------------------- #
# 崩溃窗口
# --------------------------------------------------------------------------- #


async def test_expired_lease_becomes_uncertain(guard: IdempotencyGuard, db):
    """**崩溃窗口必须显式标成不确定**，不是成功也不是失败。

    工具执行跨越数据库事务边界，所以「恰好一次」做不到；
    能做的是把不确定收敛成一个可识别的状态，让上层决定怎么办（`FR-T-08`）。
    """
    claim = await guard.claim(_inv(), "write")
    assert claim.can_execute

    # 让租约过期：直接把 started_at 看成一小时前
    async with db.transaction() as tx:
        view = await ToolExecutionRepo(tx).find(
            "s-1", claim.idem_key, lease_s=60, now=datetime.now(timezone.utc) + timedelta(hours=1)
        )
    assert view is not None
    assert view.state == "uncertain", "超租约的 in_flight 必须判成 uncertain"
    assert not view.done and not view.retryable, (
        "uncertain 既不是成功也不是可重试的失败 —— 它需要人来判断"
    )


async def test_uncertain_is_not_written_back(guard: IdempotencyGuard, db):
    """**读取时的判定不得写回数据库。**

    写回是破坏性的（``in_flight`` 改成 ``uncertain`` 之后就回不去了），
    而且迟到的 ``done`` 就再也盖不住它。这正是 ``find`` 返回只读视图的原因 ——
    直接改 ORM 对象的属性会让它变脏，脏跟踪会在提交时真的落库。
    """
    claim = await guard.claim(_inv(), "write")

    async with db.transaction() as tx:
        await ToolExecutionRepo(tx).find(
            "s-1", claim.idem_key, lease_s=60, now=datetime.now(timezone.utc) + timedelta(hours=1)
        )

    async with db.transaction() as tx:
        raw = await ToolExecutionRepo(tx)._load("s-1", claim.idem_key)
    assert raw is not None and raw.state == "in_flight", (
        "库里必须还是 in_flight —— 判定只发生在读取时"
    )


async def test_failed_is_retryable(guard: IdempotencyGuard):
    """失败**不是终态**：``failed`` 之后可以重新登记。"""
    first = await guard.claim(_inv(), "write")
    await guard.mark_failed(first, "网络断了")

    again = await guard.claim(_inv(), "write")
    assert again.can_execute, "失败必须能重试 —— 否则一次偶发故障会把那个键永久锁死"


# --------------------------------------------------------------------------- #
# Redis 不可用 —— **不是降级，是慢路径**
# --------------------------------------------------------------------------- #


async def test_redis_down_falls_back_to_authoritative_record_only(store, db):
    """**Redis 挂了不等于幂等失效。**

    这是实现阶段推翻的一处设计（初稿写的是「Redis 不可用就拒绝写入」）。
    顺着一个失败用例查下去发现：初稿太保守 ——

        唯一约束 ``(scope, idem_key)`` **本身就是正确的并发闸门**。
        Redis 只是它的快路径。所以 Redis 不在时，正确的做法是退到
        「只用权威记录」—— 慢一点（每次多一次 SELECT + INSERT），**语义完全正确**，
        而不是让整个 agent 停工。

    「Redis 的可用性等价于平台可用性」是这个模块不该背的代价。
    """
    store.set_available(False)
    guard = IdempotencyGuard(store, db)

    first = await guard.claim(_inv(), "write")
    assert first.can_execute, "Redis 不在也要能干活 —— 权威记录足以保证幂等"
    assert first.outcome == "executed", "这不是降级执行，是慢路径执行"

    await guard.mark_done(first, ToolResult(outcome="executed", tool_name="echo", output="ok"))
    again = await guard.claim(_inv(), "write")
    assert not again.can_execute and again.outcome == "reused"


async def test_mid_call_redis_failure_falls_back_too(store, db):
    """**调用中途** Redis 断了，同样退到慢路径，而不是报成执行失败。

    报失败会诱导上层重试 —— 而重试恰恰是最需要幂等保护的动作。
    """
    guard = IdempotencyGuard(store, db)
    store.fail_next_claim = True

    claim = await guard.claim(_inv(), "write")
    assert claim.can_execute
    assert claim.outcome == "executed"


async def test_unavailable_store_still_works_with_a_database(db):
    """没有 Redis 的部署（CLI 默认）：一样能干活，只是每次多一次库往返。"""
    guard = IdempotencyGuard(UnavailableStore(), db)
    assert (await guard.claim(_inv(), "write")).can_execute


async def test_without_authority_write_is_refused(store):
    """**没有权威记录才是真的没有幂等**（`DT-3`）。

    Redis 只是缓存 —— 清一次库就静默地变成可以重复执行。所以这种组合下
    有副作用的工具必须被拒。
    """
    guard = IdempotencyGuard(store, None)
    claim = await guard.claim(_inv(), "write")
    assert not claim.can_execute
    assert claim.outcome == "refused"
    assert "没有配置数据库" in claim.reason


async def test_without_authority_read_is_allowed_but_degraded(store):
    """**只读放行，但必须是独立的档位。**

    只读重复的代价是「浪费一次 IO」，为了它让整个 agent 停工代价不成比例。
    但放行**必须可区分**（``executed_degraded`` ≠ ``executed``）——
    否则「偶尔出现的重复执行」永远查不出原因。
    """
    guard = IdempotencyGuard(store, None)
    claim = await guard.claim(_inv(), "read")
    assert claim.can_execute
    assert claim.outcome == "executed_degraded"
    assert claim.degraded


# --------------------------------------------------------------------------- #
# 结果缓存的大小
# --------------------------------------------------------------------------- #


async def test_oversized_result_is_not_cached_but_dedup_still_works(store, db):
    """结果超出上限时**不缓存内容**，但**去重仍生效**。

    假装复用了、返回一个被截断的结果，会让下游基于不完整信息做判断 ——
    那比明说「结果太大没缓存」更糟。
    """
    guard = IdempotencyGuard(store, db, max_cached_result_bytes=16)
    claim = await guard.claim(_inv(), "read")
    await guard.mark_done(
        claim, ToolResult(outcome="executed", tool_name="echo", output="x" * 1000)
    )

    again = await guard.claim(_inv(), "read")
    assert not again.can_execute, "去重必须仍然生效"
    assert again.outcome == "reused"
    assert again.reused_output == "", "超限的结果不缓存内容"
    assert again.reused_truncated, "并且要标明「没能缓存」，不能假装复用了完整结果"


# --------------------------------------------------------------------------- #
# 唯一约束 —— 最后一道保险
# --------------------------------------------------------------------------- #


async def test_unique_constraint_rejects_the_second_registration(db):
    """并发登记时数据库只让一个成功。

    比「先查再插」可靠：后者在并发下有竞态窗口，而且**不会报错**。
    """
    from repo.tool import ToolExecutionRepo

    async with db.transaction() as tx:
        first = await ToolExecutionRepo(tx).register(
            scope="s-1", idem_key="k", tool_name="echo", side_effect="write",
            args_digest="d", owner="o1",
        )
    async with db.transaction() as tx:
        second = await ToolExecutionRepo(tx).register(
            scope="s-1", idem_key="k", tool_name="echo", side_effect="write",
            args_digest="d", owner="o2",
        )

    assert first is True
    assert second is False, "冲突必须被翻译成 False，而不是抛异常"

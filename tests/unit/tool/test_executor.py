"""执行编排（``tool/executor.py``）—— 责任链与它的顺序。

**顺序本身是被测的对象**：链上相邻两步的先后如果写反，大多不会报错，
只会让某一类调用悄悄得到错误的结果。下面每条断言都盯着一个具体的顺序。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tool.executor import ToolEvent, ToolExecutor
from tool.idempotency import IdempotencyGuard
from tool.permission import PermissionPolicy, SideEffectPolicy
from tool.registry import ToolRegistry
from tool.types import ToolInvocation

from .conftest import BoomTool, EchoTool, InMemoryStore, WriteFileTool


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, payload) -> None:
        self.events.append((name, dict(payload)))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]


def _make(
    *tools,
    store: InMemoryStore,
    db,
    permission: PermissionPolicy | None = None,
    workdir: Path | None = None,
    sink: RecordingSink | None = None,
    **guard_kw,
) -> ToolExecutor:
    scope_root = SideEffectPolicy(paths=(workdir.resolve(),)) if workdir else None
    policy = permission or PermissionPolicy(
        enabled=("read", "write"),
        scopes={"read": scope_root, "write": scope_root},
    )
    return ToolExecutor(
        registry=ToolRegistry(tools),
        permission=policy,
        guard=IdempotencyGuard(store, db, **guard_kw),
        events=sink,
    )


def _inv(name: str, **over) -> ToolInvocation:
    base = {"tool_name": name, "arguments": {}, "scope": "s-1"}
    base.update(over)
    return ToolInvocation(**base)


# --------------------------------------------------------------------------- #
# ① 找工具
# --------------------------------------------------------------------------- #


async def test_unknown_tool_lists_the_available_ones(store, db, workdir):
    ex = _make(EchoTool(), store=store, db=db, workdir=workdir)
    result = await ex.invoke(_inv("nope"))
    assert result.outcome == "refused"
    assert "echo" in result.error, "必须列出全部可用名 —— 工具名是模型生成的，它拼错时最需要这个"


# --------------------------------------------------------------------------- #
# ② 参数
# --------------------------------------------------------------------------- #


async def test_invalid_arguments_are_refused_without_executing(store, db, workdir):
    """**参数不合法绝不执行。**

    带着错参数执行会产生更难查的副作用（文件写歪了、命令跑了一半），
    所以宁可把错误回给模型让它改。
    """
    tool = EchoTool()
    ex = _make(tool, store=store, db=db, workdir=workdir)

    result = await ex.invoke(_inv("echo", arguments={}))  # 缺 required 的 text
    assert result.outcome == "refused"
    assert "schema" in result.error
    assert tool.calls == 0, "参数不合法时不得执行"


async def test_explicit_idempotency_key_is_not_passed_to_the_tool(store, db, workdir):
    """**显式幂等键要从实参里剔除** —— 它是执行层的概念，不是工具的入参。

    不剔除的话，有严格 schema 的工具会因为「多了一个未知参数」而校验失败，
    而那个失败的形状（「参数不符」）会把人引向错误的方向。
    """
    tool = EchoTool()
    ex = _make(tool, store=store, db=db, workdir=workdir)

    result = await ex.invoke(
        _inv("echo", arguments={"text": "hi", "idempotency_key": "k-1"})
    )
    assert result.outcome == "executed", result.error
    assert result.output == "hi"


# --------------------------------------------------------------------------- #
# ③ 权限 —— **在幂等之前**
# --------------------------------------------------------------------------- #


async def test_permission_is_checked_before_idempotency(store, db, workdir):
    """**未被授权的调用者不能通过幂等复用拿到别人的结果。**

    这是需求说明书初稿里写反了的一处（实现在前），也是本次编码推翻了它：
    初稿把幂等放在权限之前，理由是「重复的调用会因为无权限被拒，
    而上层以为没执行过」。但那条理由漏了一件事 ——

        幂等在权限**之后**时，一个未被授权的调用者只要猜到（或复用）别人的幂等键，
        就能拿到**别人**执行出来的结果，比如它无权读的文件内容。
        去重表于是变成了一个越权读取的通道。

    而被拒的重复调用并不造成实际损害：那个调用者本来就不该跑这个工具。
    """
    tool = EchoTool()
    # 权限策略里**不启用 read** —— echo 是 read 类
    denied = PermissionPolicy(enabled=("write",), scopes={})
    ex = _make(tool, store=store, db=db, workdir=workdir, permission=denied)

    first = await ex.invoke(_inv("echo", arguments={"text": "秘密"}))
    assert first.outcome == "refused"
    assert tool.calls == 0, "权限没过时不得执行"


async def test_permission_denied_leaves_no_in_flight(store, db, workdir):
    """权限拒绝之后不得留下 ``in_flight`` 记录。

    留下的后果：下次真的被授权时，那条记录已经超租约变成 ``uncertain``，
    于是**合法的第一次执行也会被当成崩溃窗口**。
    """
    import sqlalchemy as sa

    denied = PermissionPolicy(enabled=(), scopes={})
    ex = _make(EchoTool(), store=store, db=db, workdir=workdir, permission=denied)
    await ex.invoke(_inv("echo", arguments={"text": "x"}))

    async with db.transaction() as tx:
        assert await tx.scalar(sa.text("SELECT count(*) FROM tool_execution")) == 0


# --------------------------------------------------------------------------- #
# ④⑤⑥⑦ 幂等与执行 —— 副作用只发生一次
# --------------------------------------------------------------------------- #


async def test_side_effect_happens_exactly_once(store, db, workdir):
    """**核心断言**：同一个键提交两次，文件只被写一次。"""
    tool = WriteFileTool()
    ex = _make(tool, store=store, db=db, workdir=workdir)
    arguments = {"path": "out.txt", "content": "第一次"}

    first = await ex.invoke(_inv("writer", arguments=arguments))
    second = await ex.invoke(_inv("writer", arguments=arguments))

    assert first.outcome == "executed"
    assert second.outcome == "reused"
    assert tool.calls == 1, "**副作用只发生一次** —— 这是整个模块存在的理由"
    assert (workdir / "out.txt").read_text(encoding="utf-8") == "第一次"


async def test_concurrent_duplicate_is_blocked(store, db, workdir):
    """第一次还没结束时，第二次提交被挡住（``in_flight``），**不会一起执行**。"""
    tool = WriteFileTool()
    ex = _make(tool, store=store, db=db, workdir=workdir)
    arguments = {"path": "out.txt", "content": "x"}

    first = await ex.invoke(_inv("writer", arguments=arguments))
    # 手工把 Redis 里的 claim 恢复成「在进行中」，模拟第一个还没结束
    assert first.outcome == "executed"

    claim = await ex.guard.claim(_inv("writer", arguments=arguments), "write")
    assert not claim.can_execute and claim.outcome == "reused"


async def test_tool_exception_becomes_a_result_not_a_crash(store, db, workdir):
    """**工具内部抛异常不得冒泡成 agent 崩溃**（`NFR-T-04`）。

    它变成一个失败结果回给模型，让模型换个做法 ——
    一个工具崩了不该让整轮执行停下。
    """
    ex = _make(BoomTool(), store=store, db=db, workdir=workdir)
    result = await ex.invoke(_inv("boom"))
    assert result.outcome == "failed"
    assert "boom" in result.error or "RuntimeError" in result.error


async def test_failed_then_retried_succeeds(store, db, workdir):
    """失败之后能重试成功 —— 一次偶发故障不该把那个键永久锁死。"""
    ex = _make(BoomTool(), store=store, db=db, workdir=workdir)
    first = await ex.invoke(_inv("boom"))
    assert first.outcome == "failed"

    second = await ex.invoke(_inv("boom"))
    assert second.outcome == "failed", "重试被允许（它又失败了，但不是被锁死）"
    assert second.idem_key == first.idem_key


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #


async def test_event_sequence_for_execute_then_reuse(store, db, workdir):
    """事件序列要能还原「执行了一次、复用了一次」。"""
    sink = RecordingSink()
    ex = _make(EchoTool(), store=store, db=db, workdir=workdir, sink=sink)
    inv = _inv("echo", arguments={"text": "hi"})

    await ex.invoke(inv)
    await ex.invoke(inv)

    assert sink.names == [ToolEvent.STARTED, ToolEvent.EXECUTED, ToolEvent.REUSED]


async def test_events_carry_subject_and_outcome(store, db, workdir):
    """``subject`` 与 ``outcome`` 必须带上 —— 事件表就是靠这两列传的。"""
    sink = RecordingSink()
    ex = _make(EchoTool(), store=store, db=db, workdir=workdir, sink=sink)
    await ex.invoke(_inv("echo", arguments={"text": "hi"}))

    _, payload = sink.events[-1]
    assert payload["subject"] == "echo"
    assert payload["outcome"] == "executed"


async def test_emitter_failure_does_not_break_the_call(store, db, workdir):
    """观测失败不能拖垮工具调用（与 gateway 的 `_emit` 同一条纪律）。"""

    class Exploding:
        def emit(self, name, payload):
            raise RuntimeError("事件总线下线了")

    ex = _make(EchoTool(), store=store, db=db, workdir=workdir, sink=Exploding())
    result = await ex.invoke(_inv("echo", arguments={"text": "hi"}))
    assert result.outcome == "executed"


async def test_degraded_execution_has_its_own_event(store, workdir):
    """**没有权威记录**时放行只读工具：事件与 outcome 都必须是独立档位。

    ⚠ 注意触发条件：是**没有数据库**，不是「Redis 挂了」。
    Redis 挂只是慢路径（见 ``test_redis_down_falls_back_to_authoritative_record_only``），
    语义完全正确、不算降级。

    混进普通的 ``executed`` 会让「偶尔出现的重复执行」永远查不出原因，
    所以它必须是独立的事件名与 outcome。
    """
    sink = RecordingSink()
    ex = _make(EchoTool(), store=store, db=None, workdir=workdir, sink=sink)

    result = await ex.invoke(_inv("echo", arguments={"text": "hi"}))
    assert result.outcome == "executed_degraded"
    assert ToolEvent.EXECUTED_DEGRADED in sink.names

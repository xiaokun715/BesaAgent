"""``src/tool`` 单测的公共夹具。

**幂等存储用内存实现**（``NFR-T-06``：不得依赖真实 Redis）。
这不是为了跑得快，是为了让「Redis 中途断开」这类断言**能稳定复现** ——
真实网络下它们要么偶发，要么无法构造。

**权威记录用内存 SQLite**，因为 ``tool_execution`` 的唯一约束、
``IntegrityError`` 翻译、以及「清空 Redis 后仍能去重」这几条，
只有真的有一个数据库才测得出来。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundation.database import Database
from foundation.db import Base, create_engine
from tool.base import Tool, ToolContext
from tool.idempotency import ClaimState, IdempotencyGuard, StoreUnavailable
from tool.permission import PermissionPolicy, SideEffectPolicy
from tool.registry import ToolRegistry
from tool.types import ToolResult

# 让 tool_execution 注册到 Base.metadata（create_all 只建此刻已注册的表）
import repo.tool  # noqa: F401


class InMemoryStore:
    """幂等存储的内存实现。**只给测试用**。

    它实现的语义与 Redis 版一致（包括 ``release`` 校验持有者），
    但额外暴露几个计数与开关，用来构造真实 Redis 上难以稳定复现的场景。
    """

    def __init__(self, *, available: bool = True) -> None:
        self._claims: dict[str, str] = {}
        self._done: dict[str, str] = {}
        self._available = available
        self.claim_calls = 0
        self.release_calls = 0
        #: 置真后，claim 会抛 StoreUnavailable —— 模拟「调用中途 Redis 断了」
        self.fail_next_claim = False

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, value: bool) -> None:
        self._available = value

    def flush_redis(self) -> None:
        """模拟 Redis 被清空 —— **权威记录不动**。

        这正是「清库就静默地变成可以重复执行」那条要防的场景：
        清完之后仍不能重复执行，才算设计成立。
        """
        self._claims.clear()
        self._done.clear()

    async def claim(self, key: str, owner: str, *, lease_s: float) -> ClaimState:
        self.claim_calls += 1
        if self.fail_next_claim:
            self.fail_next_claim = False
            self._available = False
            raise StoreUnavailable("模拟：调用中途 Redis 断开")
        if not self._available:
            raise StoreUnavailable("Redis 不可用")
        if key in self._done:
            return ClaimState("done", self._done[key])
        if key in self._claims:
            return ClaimState("in_flight")
        self._claims[key] = owner
        return ClaimState("claimed")

    async def mark_done(self, key: str, payload: str, *, ttl_s: float) -> None:
        self._done[key] = payload
        self._claims.pop(key, None)

    async def release(self, key: str, owner: str) -> None:
        self.release_calls += 1
        # **校验持有者** —— 与 Redis 版的 Lua 同一条纪律
        if self._claims.get(key) == owner:
            self._claims.pop(key, None)


class EchoTool(Tool):
    """把参数原样吐回来，并记录它被执行了几次。"""

    name = "echo"
    description = "回显"
    side_effect = "read"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(outcome="executed", tool_name=self.name, output=str(args.get("text", "")))


class WriteFileTool(Tool):
    """真的写文件 —— 用来断言「副作用只发生一次」。"""

    name = "writer"
    description = "写文件"
    side_effect = "write"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls += 1
        target = Path(ctx.allowed_paths[0]) / str(args["path"])
        target.write_text(str(args["content"]), encoding="utf-8")
        return ToolResult(outcome="executed", tool_name=self.name, output=str(target))


class BoomTool(Tool):
    name = "boom"
    description = "总是抛异常"
    side_effect = "write"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("工具内部炸了")


@pytest.fixture
async def db():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    database = Database(engine)
    yield database
    await database.aclose()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def permission(workdir: Path) -> PermissionPolicy:
    """读/写都开放到临时目录，**destructive 不开放**。"""
    scope = SideEffectPolicy(paths=(workdir.resolve(),))
    return PermissionPolicy(
        enabled=("read", "write"),
        scopes={"read": scope, "write": scope},
    )


@pytest.fixture
def guard(store: InMemoryStore, db: Database) -> IdempotencyGuard:
    return IdempotencyGuard(store, db, lease_s=60, result_ttl_s=3600)

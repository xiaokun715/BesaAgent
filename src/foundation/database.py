"""统一数据库执行入口（unit of work）。

**为什么需要**：多套并存的 DB 访问约定（有的对象收 sessionmaker、有的收 engine）
会导致**一个对象无法兼任两者** —— 于是想在一次业务操作里同时用仓储和向量库，
就必须同时持有 sessionmaker 与 engine，并接受两者**不能共享连接与事务**。

**真实后果**（不是理论洁癖，besa-iv-kb 踩过）：

- ``VectorWriteStep`` 把「按文档版本删旧 + upsert」拆成**两个**事务 →
  崩在中间就向量全丢，且数据处于不一致状态；
- ``FtsWriteStep`` 是 1 次删除 + N 次逐片事务 → 片数越多，半成品窗口越大。

在 agent 场景里同类风险是：**checkpoint 落库 + 事件落库 + 用量落库**三步，
若各是一个事务，「执行到一半进程挂了」会留下互相矛盾的记录，
而断点续跑依赖的正是这三者的一致性。

**本模块提供**：**单一事务边界**，让「一次业务操作 = 一个事务」成为默认而非特例。
事务边界的开启/提交/回滚只有一份实现，业务代码不再自己 commit。

**谁用**：``src/repo`` 的全部仓储都收本模块的执行入口 ——
不收 engine、不收 sessionmaker。这是保证「一次操作一个事务」的唯一办法：
把选择的自由收走。

**与 ``db.py`` 的分工**：``db.py`` 管**怎么建**（Base、命名约定、引擎工厂），
本模块管**怎么用**（事务边界、执行入口）。

**实现期修订**：原设计把 ``Database`` / ``Transaction`` 写成 ``Protocol``，
以便内存实现能满足同一协议。编码时发现那条路走不通 ——
``Transaction.execute()`` 收的是 SQLAlchemy 语句，一个内存对象**无法**执行它，
于是「内存实现」只能是个什么都不做的空壳，而空壳会**静默丢弃写入**，
正好是本项目最不能接受的那种失败。

改成的做法是：**抽象点放在引擎/方言，而不是事务类**。
同一个 ``Database`` 跑在 ``postgresql+asyncpg`` 与 ``sqlite+aiosqlite`` 之上，
仓储代码只写一次。CLI 因此可以拿一个 SQLite 引擎，而不是一个会说谎的空壳。
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import Result
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from foundation.db import create_session_factory

__all__ = ["Database", "Transaction"]


#: 当前协程链是否已在某个 ``Database`` 的事务里。
#:
#: **为什么必须用 ContextVar 而不是实例属性**：数据库对象与并发协程是一对多的，
#: 一个 ``self._busy = True`` 会被另一个协程看见，于是并发的两个独立事务里
#: 有一个会被误判成嵌套。ContextVar 是**按协程链**隔离的，能正确区分
#: 「同一个协程链里的嵌套」与「两个并发协程各自开事务」。
_IN_TRANSACTION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "besa_in_transaction", default=None
)


class Transaction:
    """事务内的执行手段。**仓储只认它**。

    **刻意不暴露 ``commit`` / ``rollback``** —— 边界只有 :meth:`Database.transaction`
    那一处能开能关。这是「把选择的自由收走」在类接口上的样子：
    不是靠约定「你不要 commit」，而是**没有那个方法**。
    """

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------------- 读写
    async def execute(self, statement: Any, params: Mapping[str, Any] | None = None) -> Result[Any]:
        """执行一条 SQLAlchemy 语句，返回原始 ``Result``。

        取 ORM 实体用 ``(await tx.execute(stmt)).scalars()``；
        只想拿一行一列，用 :meth:`scalar` 更省事。

        ``params`` 是绑定参数。**用绑定参数而不是拼字符串** ——
        拼字符串既会引入注入面，也让数据库无法复用执行计划。
        """
        return await self._session.execute(statement, params or {})

    async def fetch_all(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> Sequence[Any]:
        """执行并返回**全部第一列**（最常用：给 ``select(Model.col)`` 用）。

        注意它**不是** ``scalars().all()`` 的通用替代 —— 对多列查询它会丢掉其余列。
        需要多列时用 :meth:`execute`。
        """
        result = await self._session.execute(statement, params or {})
        return list(result.scalars().all())

    async def fetch_one(
        self, statement: Any, params: Mapping[str, Any] | None = None
    ) -> Any | None:
        """执行并返回第一行第一列；没有则 ``None``。"""
        result = await self._session.execute(statement, params or {})
        return result.scalars().first()

    async def scalar(self, statement: Any, params: Mapping[str, Any] | None = None) -> Any:
        """执行并返回第一行第一列。**没有行时返回 ``None``** ——

        与 SQLAlchemy 的 ``scalar_one()`` 不同，它不会因「恰好没有行」而抛异常。
        查「存不存在」这类问题时，没有行是**正常结果**，不是错误。
        """
        result = await self._session.execute(statement, params or {})
        return result.scalar()

    # ---------------------------------------------------------------- 写入
    def add(self, instance: Any) -> None:
        """把 ORM 实体加入本事务。**不提交** —— 提交由边界负责。"""
        self._session.add(instance)

    def add_all(self, instances: Sequence[Any]) -> None:
        """批量加入。**不提交**。

        批量插入走这一个方法而不是循环 ``add()``，是为了让 SQLAlchemy 能把它
        编译成一条多值 ``INSERT``（``NFR-R-06``：写放大受控）——
        循环 add 在数据量大时会产生 N 条往返，而每条往返都是一次网络等待。
        """
        if instances:
            self._session.add_all(list(instances))

    async def delete(self, instance: Any) -> None:
        """删除 ORM 实体。同样**不提交**。"""
        await self._session.delete(instance)

    async def flush(self) -> None:
        """把挂起的写入推到数据库（仍**不提交**）。

        用途是拿到自增主键或触发数据库侧约束 —— 想在一个事务里
        「先写主表、再用它的 id 写子表」时需要它。
        """
        await self._session.flush()


class Database:
    """执行入口。**单一事务边界**。

    一个实例 = 一个引擎 = 一个连接池。**进程内应当是单例**
    （每个仓储各建一个池会打爆文件描述符上限 —— ``foundation/container.py`` 记过这个教训）。
    """

    __slots__ = ("_engine", "_session_factory")

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """开一个事务边界：进块即 BEGIN，正常出块 COMMIT，抛异常则 ROLLBACK。

        **禁止嵌套**。嵌套调用会各建一个会话 —— 也就是**两个独立事务**，
        正是本模块存在的理由所要消灭的那个事故形态。而它**不会报错**：
        外层提交成功、内层回滚，数据处于谁也没预期过的状态。
        所以这里把它变成一条明确的错误。

        Raises:
            RuntimeError: 在同一个协程链里嵌套调用。
        """
        holder = _IN_TRANSACTION.get()
        if holder is not None:
            raise RuntimeError(
                f"事务不能嵌套：已经在 {holder} 的事务里了，又想开第二个边界。\n"
                "嵌套会各建一个会话 = 两个独立事务，外层提交、内层回滚，"
                "而两边都不会报错 —— 这正是本模块要消灭的事故形态。\n"
                "若要在一个业务操作里写多张表，请共用同一个 `async with` 块。\n"
                "若确实需要并行的独立事务，请在事务块**之外**创建任务。"
            )

        token = _IN_TRANSACTION.set(f"{type(self).__name__}({id(self):#x})")
        try:
            async with self._session_factory() as session:
                # session.begin() 让「进块即 BEGIN、出块即 COMMIT/ROLLBACK」
                # 成为不可绕过的事实 —— 业务代码拿不到 session，也就无从 commit。
                async with session.begin():
                    yield Transaction(session)
        finally:
            _IN_TRANSACTION.reset(token)

    async def aclose(self) -> None:
        """释放连接池。**必须幂等** —— 关停路径可能重复调用。"""
        await self._engine.dispose()

    @property
    def engine(self) -> AsyncEngine:
        """底层引擎。

        ⚠ **只给装配层与 Alembic 用**。仓储不得使用它 ——
        一旦某个仓储绕过 ``transaction()`` 直接拿引擎，事务边界就漏了，
        而漏了之后不会有任何报错。这个属性存在是因为迁移与健康检查确实需要它，
        不是因为它是给人随手用的。
        """
        return self._engine

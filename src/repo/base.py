"""仓储基类 —— 全部仓储的公共部分。

**仓储只收「执行入口」，不收 engine、不收 sessionmaker**（``foundation/database.py`` 定死了这条）。
理由是「把选择的自由收走」：拿得到 engine 的人就能自己开事务，
而一旦有人开了第二个事务，「一次业务操作 = 一个事务」这条保证就悄悄没了 ——
且**不会报错**。

所以本模块的仓储签名长这样::

    class SessionRepo(Repository):
        async def add(self, ...): ...
        async def by_id(self, ...): ...

    # 用的时候：
    async with db.transaction() as tx:
        await SessionRepo(tx).add(...)
        await EventRepo(tx).append(...)      # ← 同一个事务

**为什么 ``Database`` / ``Transaction`` 从 foundation 再导出一次**：
仓储作者只需要 ``from repo.base import Repository, Transaction`` 一行，
而不必知道这两个类型住在 foundation 的哪个文件里。
"""

from __future__ import annotations

from foundation.database import Database, Transaction

__all__ = ["Database", "Repository", "Transaction"]


class Repository:
    """全部仓储的基类。

    **构造时注入事务，而不是方法参数传入**。这样「这个仓储属于哪个事务」
    在构造那一刻就定了，不需要每个方法都读一遍参数；也让「拿到的仓储必然同事务」
    成为一个可以一眼看出的性质（决策 `DR-2`）。
    """

    __slots__ = ("_tx",)

    def __init__(self, tx: Transaction) -> None:
        self._tx = tx

    @property
    def tx(self) -> Transaction:
        """本仓储所属的事务。子类通过它执行语句。"""
        return self._tx

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

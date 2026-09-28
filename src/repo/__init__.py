"""持久化层：平台**唯一**的数据库写入入口。

**本模块只认协议，不认识具体后端**：

- 执行入口（事务边界）来自 ``foundation.database``，由组合根注入；
- 向量能力的实现在 ``apps/server/storage/postgres/``，同样由组合根注入；
- 引擎的构造**不在这里**（``foundation/db.py``：「建引擎的权利只在组合根手里」）。

**依赖方向（硬约束，``NFR-R-03``）**：本模块只依赖 ``foundation``。
**不得** import ``gateway`` / ``agent`` / ``multiagent`` / ``runtime`` / ``event`` / ``memory`` / ``skill``
—— 本模块在依赖链上比它们更下游。

**尚未实现**：会话 / 消息 / 事件 / 记忆 / 断点 / 技能 / 用量 / 向量。
当前已就位的是承重部分（事务边界与类型载体），
实施顺序见 ``docs/repo/架构概要设计-repo.md`` §10。
"""

from __future__ import annotations

from repo.base import Database, Repository, Transaction
from repo.types import BatchResult, Metric, Page, TimeRange, VectorSpace

__all__ = [
    "BatchResult",
    "Database",
    "Metric",
    "Page",
    "Repository",
    "TimeRange",
    "Transaction",
    "VectorSpace",
]

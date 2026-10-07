"""把所有 ORM 模型**注册**到 ``foundation.db.Base``。

**import 本模块 = 让 ``Base.metadata`` 认识全部表。** 除此之外它什么都不做。

**为什么需要它**：有三处都要「看见全部表」，而它们的入口各不相同 ——

1. **Alembic 的 ``env.py``**：看不见表就会认为那张表**应该被删掉**，
   然后生成一个 ``DROP TABLE`` 的迁移；
2. **``create_all``**（CLI 的内存库、单测）：只建「此刻已注册」的表 ——
   漏一个，报错是「表不存在」，而那句建表明明刚刚成功过；
3. **任何要自建库的地方**。

让这三处各自维护一份 import 列表，就是让它们**各自有机会漏一个**。
收在这里之后，新增一张表只改一处。

**与 Alembic 的关系**：``migrations/env.py`` import 本模块而不是逐个 import ——
它是「全部表」这件事的唯一事实来源。这与 ``foundation/db.py`` 持有唯一那份
``Base`` 与命名约定是同一条纪律。
"""

from __future__ import annotations

# 下面这些 import **是有副作用的**（把表注册到 Base.metadata），
# 所以不能因为「看着没用到」就删掉。
from repo.event import EventRow
from repo.tool import ToolExecutionRow
from repo.usage import UsageDropRow, UsageRow

__all__ = ["EventRow", "ToolExecutionRow", "UsageDropRow", "UsageRow"]

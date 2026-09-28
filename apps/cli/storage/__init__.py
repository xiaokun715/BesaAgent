"""CLI 的存储策略：**默认不需要任何外部服务**。

CLI 与 server 的存储目录是**并列**的，这个并列本身在表达一件事实：
**同一个项目里，不同的 app 可以有不同的存储策略**。

- ``apps/server/storage``：Postgres + Redis，多进程、长期运行；
- ``apps/cli/storage``：默认一个**内存** SQLite，进程退出即丢，不依赖任何外部服务。

**为什么 CLI 不直接不落库**：因为「不落库」与「落在内存里」在代码上是**两条路径**，
而两条路径会漂移。让 CLI 拿一个真的数据库（只是它在内存里），
仓储代码就与 server 完全同一条 —— 而项目级前提「无需数据库即可跑通」仍然成立。

**为什么不是 SQLite 文件**：CLI 的默认场景是「问一句、看答案、退出」，
把这种一次性调用的痕迹写进磁盘没有收益，只有清理负担。
需要留存时显式给 ``--dsn`` 或交给 server。
"""

from __future__ import annotations

from apps.cli.storage.sqlite import MEMORY_DSN, open_database

__all__ = ["MEMORY_DSN", "open_database"]

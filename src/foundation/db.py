"""SQLAlchemy Base、命名约定与异步引擎/会话工厂（供 Alembic 与运行时共用）。

**谁用**：Alembic 的迁移环境与运行时共用同一份 Base 与引擎装配，避免「迁移看到的表」
和「运行时看到的表」不一致。

**谁不用**：``src/repo`` 的仓储**不自己建引擎** —— 它们接收注入了执行入口的实例
（见 ``foundation/database.py``）。建引擎的权利只在组合根手里。

**为什么命名约定要在这里统一定义**：约束名（primary key / foreign key / unique / index）
若各处不一致，Alembic 的 autogenerate 会反复产生「重命名约束」的**假 diff**，
迁移文件越堆越乱，最后没人敢跑 autogenerate。这是可以一次性避免的长期成本。

**依赖边界**：本模块允许依赖 sqlalchemy / asyncpg，**不 import** 任何
``src/repo`` 或业务模块 —— 它就是「引擎怎么建」这一件事的事实来源。
"""

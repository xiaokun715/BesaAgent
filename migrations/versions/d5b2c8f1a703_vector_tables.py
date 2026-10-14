"""vector tables

Revision ID: d5b2c8f1a703
Revises: c3f1a7b2d9e4
Create Date: 2026-10-07 14:00:00.000000+00:00

建 ``vector_spaces`` 与三张按维度分表的 ``embeddings_<dim>``（`FR-R-08`，
设计见 ``docs/vector/架构概要设计-vector.md``）。

**为什么在迁移里再定义一遍 ``vector(N)`` 类型，而不 import 应用代码里的**：
迁移必须**自包含** —— 它要保证「三年后重跑这个迁移，建出来的 DDL 一模一样」。
引 import 应用层的类型，等于让应用层的重构有机会改掉历史迁移的行为。

**`CREATE EXTENSION` 的位置**：按 `FR-R-10` 放这里，且幂等。
实测 ``vector.control`` 没有 ``trusted = true``，所以**首次部署需要超级用户先建一次**；
扩展已存在时，本项目角色跑 ``IF NOT EXISTS`` 能通过（见 ``RR-6``）。
**不静默跳过**：没有权限且扩展不存在时它会直接报错，这是刻意的 ——
否则后续迁移会在一个没有向量能力的库上"成功"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'd5b2c8f1a703'
down_revision: str | None = 'c3f1a7b2d9e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 支持的维度。**白名单**（`DV-2`）：新增维度要加迁移，不由运行时动态建表。
#:
#: ⚠ **不含 4096** —— pgvector 的 HNSW 索引上限是 2000 维，实测报错原文：
#: ``column cannot have more than 2000 dimensions for hnsw index``。
#: 建一张索引建不出来的表，得到的是「检索悄悄变慢」，比失败难查得多。
DIMS: tuple[int, ...] = (768, 1024)

#: HNSW 参数（与 vector.py 里的保持一致；两边都变才算改对了）。
_HNSW_WITH = {"m": 16, "ef_construction": 64}


class _Vector(sa.types.UserDefinedType):
    """迁移专用：只负责吐出 ``vector(N)`` 这个列类型。"""

    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: object) -> str:
        return f"vector({self.dim})"


def upgrade() -> None:
    # 幂等；没有权限且扩展不存在时会直接报错（见模块 docstring）。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        'vector_spaces',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('space', sa.Text(), nullable=False),
        sa.Column('dim', sa.Integer(), nullable=False),
        sa.Column('metric', sa.String(length=16), nullable=False),
        sa.Column('model_key', sa.Text(), nullable=False),
        sa.Column('index_options', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_vector_spaces')),
        sa.UniqueConstraint('space', name='uq_vector_spaces_space'),
    )

    for dim in DIMS:
        table = f"embeddings_{dim}"
        op.create_table(
            table,
            sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
            sa.Column('space', sa.Text(), nullable=False),
            sa.Column('owner_kind', sa.String(length=32), nullable=False),
            sa.Column('owner_id', sa.Text(), nullable=False),
            sa.Column('model_key', sa.Text(), nullable=False),
            sa.Column('embedding', _Vector(dim), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id', name=op.f(f'pk_{table}')),
            # upsert 的锚：一条 owner 在一个空间里只有一行向量。
            sa.UniqueConstraint('space', 'owner_kind', 'owner_id', name=f'uq_{table}_owner'),
        )
        # HNSW：边写边查（agent 一跑起来就持续写记忆），IVFFlat 需要先有数据再训练。
        #
        # ⚠ 构建受 maintenance_work_mem 限制。表变大后默认值（通常 64MB）
        #   会让它走磁盘排序，慢到不可接受 —— 运维步骤见设计文档 §6：
        #   SET maintenance_work_mem = '512MB';   -- 仅当前会话，建完即弃
        op.create_index(
            f'ix_{table}_hnsw',
            table,
            ['embedding'],
            unique=False,
            postgresql_using='hnsw',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
            postgresql_with=dict(_HNSW_WITH),
        )


def downgrade() -> None:
    for dim in reversed(DIMS):
        table = f"embeddings_{dim}"
        op.drop_index(f'ix_{table}_hnsw', table_name=table)
        op.drop_table(table)
    op.drop_table('vector_spaces')
    # **不删 vector 扩展**：它可能被别的表用到，而「删扩展」会连带删掉依赖它的列。

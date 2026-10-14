"""memory tables

Revision ID: c3f1a7b2d9e4
Revises: b74140cb5761
Create Date: 2026-10-07 12:00:00.000000+00:00

建 ``memory`` 与 ``memory_op_log`` 两张表（`FR-R-04` / 架构文档 §4）。

**为什么只有两张表**：需求里的「六组字段」是**逻辑分组**，不是六张表 ——
拆表会让热路径检索多几次 join。向量必须独立（``vector(N)`` 钉维度，`DR-4`），
轨迹必须独立（append-only），其余都是 ``memory`` 的列。

**本迁移不建向量表**：``embeddings_<dim>`` 的列类型是 pgvector 的 ``vector(N)``，
表模型归 ``apps/server/storage/postgres/vector.py``（尚未实现），
连同 ``CREATE EXTENSION vector`` 一起由那份迁移负责（`FR-R-10`）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3f1a7b2d9e4'
down_revision: str | None = 'b74140cb5761'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'memory',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        # ① 基础组件与租户隔离
        sa.Column('memory_id', sa.Text(), nullable=False),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('owner_id', sa.Text(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        # ② 核心记忆表示
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('content_hash', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        # ③ 记忆分类与属性
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('category', sa.Text(), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('sensitivity', sa.String(length=16), nullable=False),
        sa.Column('source_kind', sa.Text(), nullable=False),
        sa.Column('source_session_id', sa.Text(), nullable=True),
        sa.Column('source_agent', sa.Text(), nullable=True),
        sa.Column('source_trace_id', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('importance', sa.Float(), nullable=False),
        # ⑤ 版本与时效控制
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('supersedes_id', sa.Text(), nullable=True),
        sa.Column('superseded_by', sa.Text(), nullable=True),
        sa.Column('merged_into', sa.Text(), nullable=True),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        # ⑥ 访问热度
        sa.Column('access_count', sa.Integer(), nullable=False),
        sa.Column('used_count', sa.Integer(), nullable=False),
        sa.Column('evidence_count', sa.Integer(), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_decayed_at', sa.DateTime(timezone=True), nullable=True),
        # 归属与时间
        sa.Column('session_id', sa.Text(), nullable=True),
        sa.Column('caller', sa.Text(), nullable=True),
        sa.Column('meta', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_memory')),
        sa.UniqueConstraint('memory_id', name='uq_memory_memory_id'),
    )
    op.create_index(
        'ix_memory_scope_kind_state', 'memory',
        ['tenant_id', 'scope', 'kind', 'state', 'created_at'], unique=False,
    )
    op.create_index('ix_memory_state_accessed', 'memory', ['state', 'last_accessed_at'], unique=False)
    op.create_index('ix_memory_hash', 'memory', ['scope', 'content_hash'], unique=False)
    op.create_index('ix_memory_source_session', 'memory', ['source_session_id'], unique=False)

    op.create_table(
        'memory_op_log',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('op_id', sa.Text(), nullable=False),
        sa.Column('memory_id', sa.Text(), nullable=True),
        sa.Column('op', sa.String(length=16), nullable=False),
        sa.Column('before_hash', sa.Text(), nullable=False),
        sa.Column('after_hash', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('actor', sa.Text(), nullable=False),
        sa.Column('source_trace_id', sa.Text(), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('tenant_id', sa.Text(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_memory_op_log')),
        sa.UniqueConstraint('op_id', name='uq_memory_op_log_op_id'),
    )
    op.create_index('ix_memory_op_memory_time', 'memory_op_log', ['memory_id', 'occurred_at'], unique=False)
    op.create_index('ix_memory_op_op_time', 'memory_op_log', ['op', 'occurred_at'], unique=False)
    op.create_index('ix_memory_op_scope_time', 'memory_op_log', ['scope', 'occurred_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_memory_op_scope_time', table_name='memory_op_log')
    op.drop_index('ix_memory_op_op_time', table_name='memory_op_log')
    op.drop_index('ix_memory_op_memory_time', table_name='memory_op_log')
    op.drop_table('memory_op_log')

    op.drop_index('ix_memory_source_session', table_name='memory')
    op.drop_index('ix_memory_hash', table_name='memory')
    op.drop_index('ix_memory_state_accessed', table_name='memory')
    op.drop_index('ix_memory_scope_kind_state', table_name='memory')
    op.drop_table('memory')

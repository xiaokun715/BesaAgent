"""event: model_key → subject，并加 outcome

Revision ID: 5a3ce69ec387
Revises: b520bdbd3c11
Create Date: 2026-09-28 18:01:46.225125+00:00

**这一版是手工改过的**，autogenerate 的原稿有两处不能直接用：

1. 它把改名识别成 ``drop_column('model_key')`` + ``add_column('subject')`` ——
   **那会把已有数据丢掉**。改名必须用 ``alter_column(new_column_name=...)``。
   （这张表还新、几乎没有数据，但「改名用改名」这条不该因为现在就我们一个人用而放低。）
2. ``add_column(nullable=False)`` 不带默认值时，**表里只要有行就会失败**，
   而报错是「不能加 NOT NULL 列」，看不出是「你漏了默认值」。
   所以先带 ``server_default`` 加列，再立刻把它去掉 —— 让列的最终形态
   与模型（只有 Python 侧 ``default``）一致，否则
   ``compare_server_default=True`` 会反复生成「改默认值」的假迁移。

改名与新增的理由见 ``docs/tool/架构概要设计-tool.md`` 的 ``DT-10``：
平台有了第二个事件生产者（``src/tool``），而「这次动作的对象」
对模型事件是模型键、对工具事件是工具名 —— 并存两列会逼第三类生产者
在两个都不合适的名字之间二选一。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '5a3ce69ec387'
down_revision: str | None = 'b520bdbd3c11'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. 改名（保留数据）
    op.alter_column("event", "model_key", new_column_name="subject")

    # 2. 加列：先带默认值以便已有行通过 NOT NULL，再撤掉默认值
    op.add_column(
        "event", sa.Column("outcome", sa.Text(), nullable=False, server_default="")
    )
    op.alter_column("event", "outcome", server_default=None)

    # 3. 「这个会话里 bash 跑了多少次 / 哪些工具被幂等短路了」直接落在这两列上
    op.create_index("ix_event_subject_outcome", "event", ["subject", "outcome"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_event_subject_outcome", table_name="event")
    op.drop_column("event", "outcome")
    op.alter_column("event", "subject", new_column_name="model_key")

"""上下文修复 v2.1：运行请求携带适用约束快照（A05，数据契约 §3.3）。

变更内容：

1. `runs.context_snapshot`（jsonb，默认 `'{}'`）：提交分析时由服务端写入**适用事实快照**
   `{constraints: [{fact_id, kind, value, scope, scope_id, source_ref}]}`；worker 的撰稿节点
   读取同一份快照，不再临时猜取另一次会话状态。
2. 旧行默认 `{}`，worker 按"没有约束"处理（行为与之前一致）；不修改 0008 / 0009 / 0010。

**downgrade 只回滚 schema**：已写入的约束快照随列删除，不可恢复。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "context_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "context_snapshot")

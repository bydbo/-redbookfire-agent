"""runs 表新增 prompt_versions（prompt 契约 §五 / 数据契约 §3.3）。

记录本次运行各任务实际使用的 prompt 版本，是"同一批输入重测"可对比的前提：
形状如 {"hotspot_clue": 1, "material_select": 1, "copy_draft": 1}，未参与的任务不出现。
默认空对象——接线前（S3.1 / S3.4）所有运行落下的都是 {}。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("prompt_versions", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runs", "prompt_versions")
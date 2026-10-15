"""运行阶段快照：runs 加 stage（S7.20）。

- 只加一列 `stage jsonb not null default '{}'`，不动既有数据与索引；旧行读出来是空对象。
- 快照结构与合并口径见 `src/xhs_agent/core/progress.py`：`{stage, label, status, started_at,
  updated_at, attempt, retries, error_category, last_finished_stage, run_id, job_id, nodes}`；
  只记阶段切换（素材同步 / 向量回填 / 五节点），不逐 token、不逐轮询。
- `runs.status` 与 `run_hotspots` 的既有语义完全不变；快照只回答"正在哪个环节、停了多久"。
- **downgrade 只回滚 schema，快照数据不可恢复**（删列即丢），不描述为数据完整恢复。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs",
                  sa.Column("stage", postgresql.JSONB(astext_type=sa.Text()),
                            server_default=sa.text("'{}'"), nullable=False))


def downgrade() -> None:
    op.drop_column("runs", "stage")

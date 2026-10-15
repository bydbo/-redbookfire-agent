"""对话消息的轮次关联与阶段快照：chat_messages 加 turn_id / trace_id / progress（S7.21）。

- 三列都可空 / 有默认值，不动既有数据与索引；旧行读出来是 `NULL` / `NULL` / `{}`。
- `turn_id`：本轮对话标识（**不加外键**，与 `run_id` 同一口径）；`trace_id`：本轮的 Langfuse
  trace id，追踪关闭时为 NULL；`progress`：当前阶段快照，结构与 `runs.stage` 相同
  （见 `src/xhs_agent/core/progress.py`），只记阶段切换、不逐 token。
- 过期判定复用 `[chat].turn_stale_seconds`（读会话时把过期的 `running` 行标 `interrupted`，
  快照状态一并标 `interrupted`），不新增配置项。
- **downgrade 只回滚 schema，turn_id / trace_id / 快照数据不可恢复**（删列即丢），
  不描述为数据完整恢复。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("turn_id", sa.Uuid(), nullable=True))
    op.add_column("chat_messages", sa.Column("trace_id", sa.Text(), nullable=True))
    op.add_column("chat_messages",
                  sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()),
                            server_default=sa.text("'{}'"), nullable=False))


def downgrade() -> None:
    op.drop_column("chat_messages", "progress")
    op.drop_column("chat_messages", "trace_id")
    op.drop_column("chat_messages", "turn_id")

"""对话消息的轮级用量：chat_messages 加 prompt_tokens / completion_tokens（S7.10）。

- 只加两列（`integer not null default 0`），不动既有数据与索引；旧行读出来是 0。
- 两列与 `cost_cny` 同一口径：一轮对话的模型用量之和（supervisor + 工具内部调用 + 本轮分析），
  上限门槛 ≤ 0.25 元/轮（含一次分析，S7.12）。
- CHECK 与既有口径一致（`>= 0`），手写进迁移——Alembic autogenerate 不对比 CHECK（与 0003/0004 同）。

downgrade 反序删两列。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages",
                  sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"),
                            nullable=False))
    op.add_column("chat_messages",
                  sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"),
                            nullable=False))
    op.create_check_constraint(op.f("ck_chat_messages_prompt_tokens_non_negative"),
                               "chat_messages", "prompt_tokens >= 0")
    op.create_check_constraint(op.f("ck_chat_messages_completion_tokens_non_negative"),
                               "chat_messages", "completion_tokens >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_chat_messages_completion_tokens_non_negative"),
                       "chat_messages", type_="check")
    op.drop_constraint(op.f("ck_chat_messages_prompt_tokens_non_negative"),
                       "chat_messages", type_="check")
    op.drop_column("chat_messages", "completion_tokens")
    op.drop_column("chat_messages", "prompt_tokens")

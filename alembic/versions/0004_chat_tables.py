"""对话表：chat_sessions / chat_messages（数据契约 §3.6 / §3.7，S7.2）。

- 两张新表，不动既有五张表；索引与 CHECK 一并手写（Alembic autogenerate 不对比 CHECK，
  只改 ORM 会留下漂移，与 0003 的处理一致）。
- `chat_messages.run_id` **只做弱关联、不加外键**：run 被删不该动历史消息。
- `chat_sessions.updated_at` 是普通 B-tree 索引（Postgres 可反向扫描，按 `updated_at desc`
  取会话列表照样走索引）。

downgrade 反序删两张表与它们的索引（消息先删，避免外键悬空）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_sessions")),
    )
    op.create_index("ix_chat_sessions_updated_at", "chat_sessions", ["updated_at"],
                    unique=False)

    op.create_table(
        "chat_messages",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("session_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'succeeded'"), nullable=False),
        sa.Column("attachments", JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("tool_calls", JSONB(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("prompt_versions", JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("cost_cny", sa.Numeric(10, 4), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')",
                           name=op.f("ck_chat_messages_role_allowed")),
        sa.CheckConstraint("status IN ('running', 'succeeded', 'failed', 'interrupted')",
                           name=op.f("ck_chat_messages_status_allowed")),
        sa.CheckConstraint("cost_cny >= 0", name=op.f("ck_chat_messages_cost_non_negative")),
        sa.CheckConstraint("latency_ms >= 0",
                           name=op.f("ck_chat_messages_latency_non_negative")),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"],
                                name=op.f("fk_chat_messages_session_id_chat_sessions"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    op.create_index("ix_chat_messages_session_id_created_at", "chat_messages",
                    ["session_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_id_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_updated_at", table_name="chat_sessions")
    op.drop_table("chat_sessions")

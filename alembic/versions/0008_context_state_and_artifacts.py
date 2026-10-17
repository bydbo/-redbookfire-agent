"""上下文状态、产物版本、图片缓存与观测：四张新表 + chat_sessions 五个新列（E7 第六批 S7.24–S7.28）。

变更内容（《数据契约》§3.6 / §3.8–§3.11）：

1. `chat_sessions` 增加 `state_version` / `topic_id` / `context_state` / `artifact_heads` /
   `recovery_status`——全部带默认值，旧会话读出来是 `0` / `NULL` / `{}` / `{}` / `'complete'`；
2. `chat_context_events`：状态审计，`(session_id, state_version)` 唯一；
3. `chat_artifact_versions`：请求认领 + 不可变成功版本；`(session_id, request_key)` 唯一，
   另加**部分唯一索引**（`status='succeeded'` 时才约束 `version_no`）——失败记录不占成功号；
4. `chat_image_parse_cache`：按内容身份复用，`(session_id, cache_key)` 唯一，`expires_at` 索引；
5. `context_observations`：稳定 `event_id` 主键，会话 / 事件类型 + 时间索引。

**downgrade 只回滚 schema**：删列 / 删表会丢状态、审计、版本与缓存历史，不能称为完整数据恢复；
真实数据回滚必须另做备份并单独授权（与 `0006` / `0007` 同一口径）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- chat_sessions：兼容性新增（全部有默认值） ----
    op.add_column(
        "chat_sessions",
        sa.Column("state_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("chat_sessions", sa.Column("topic_id", sa.Text(), nullable=True))
    op.add_column(
        "chat_sessions",
        sa.Column("context_state", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("artifact_heads", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("recovery_status", sa.Text(),
                  server_default=sa.text("'complete'"), nullable=False),
    )
    op.create_check_constraint(
        "state_version_non_negative", "chat_sessions", sa.text("state_version >= 0")
    )
    op.create_check_constraint(
        "recovery_status_allowed", "chat_sessions",
        sa.text("recovery_status IN ('complete', 'partial')"),
    )

    # ---- chat_context_events ----
    op.create_table(
        "chat_context_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("source_ref", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("decisions", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'"), nullable=False),
        sa.Column("delta", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "state_version",
                            name="uq_chat_context_events_session_version"),
        sa.CheckConstraint("state_version >= 0", name="state_version_non_negative"),
        sa.CheckConstraint(
            "event_type IN ('delta', 'confirm', 'reject', 'retract', 'replace', "
            "'reset_topic', 'recovery')",
            name="event_type_allowed",
        ),
        sa.CheckConstraint("actor IN ('extract', 'user', 'system')", name="actor_allowed"),
    )
    op.create_index("ix_chat_context_events_session_created_at", "chat_context_events",
                    ["session_id", "created_at"])

    # ---- chat_artifact_versions ----
    op.create_table(
        "chat_artifact_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("hotspot_position", sa.Integer(), nullable=True),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=True),
        sa.Column("base_version_id", sa.Uuid(), nullable=True),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("prompt_versions", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "request_key",
                            name="uq_chat_artifact_versions_session_request"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'refused', 'interrupted')",
            name="status_allowed",
        ),
        sa.CheckConstraint("object_type IN ('draft', 'clue')", name="object_type_allowed"),
        sa.CheckConstraint("version_no IS NULL OR version_no >= 1", name="version_no_positive"),
        sa.CheckConstraint("(status = 'succeeded') = (version_no IS NOT NULL)",
                           name="succeeded_has_version"),
        sa.CheckConstraint("base_version_id IS NULL OR base_version_id <> id",
                           name="base_not_self"),
        sa.CheckConstraint("hotspot_position IS NULL OR hotspot_position >= 1",
                           name="position_positive"),
    )
    op.create_index(
        "uq_chat_artifact_versions_object_version",
        "chat_artifact_versions",
        ["session_id", "object_key", "version_no"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "ix_chat_artifact_versions_object_version",
        "chat_artifact_versions",
        ["session_id", "object_key", "version_no"],
    )

    # ---- chat_image_parse_cache ----
    op.create_table(
        "chat_image_parse_cache",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("identity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "cache_key",
                            name="uq_chat_image_parse_cache_session_key"),
        sa.CheckConstraint("expires_at > created_at", name="expires_after_created"),
    )
    op.create_index("ix_chat_image_parse_cache_expires_at", "chat_image_parse_cache",
                    ["expires_at"])

    # ---- context_observations ----
    op.create_table(
        "context_observations",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.Text(), nullable=True),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("policy_version", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint(
            "event_type IN ('context_state_decision', 'context_assembled', "
            "'artifact_version_committed', 'cache_lookup_finished', "
            "'artifact_consumed', 'model_attempt_finished')",
            name="event_type_allowed",
        ),
        sa.CheckConstraint("attempt IS NULL OR attempt >= 1", name="attempt_positive"),
    )
    op.create_index("ix_context_observations_session_created_at", "context_observations",
                    ["session_id", "created_at"])
    op.create_index("ix_context_observations_type_created_at", "context_observations",
                    ["event_type", "created_at"])


def downgrade() -> None:
    # 只回滚 schema：状态、审计、版本与观测数据随表 / 列删除，不可恢复
    op.drop_index("ix_context_observations_type_created_at", table_name="context_observations")
    op.drop_index("ix_context_observations_session_created_at", table_name="context_observations")
    op.drop_table("context_observations")

    op.drop_index("ix_chat_image_parse_cache_expires_at", table_name="chat_image_parse_cache")
    op.drop_table("chat_image_parse_cache")

    op.drop_index("ix_chat_artifact_versions_object_version",
                  table_name="chat_artifact_versions")
    op.drop_index("uq_chat_artifact_versions_object_version",
                  table_name="chat_artifact_versions")
    op.drop_table("chat_artifact_versions")

    op.drop_index("ix_chat_context_events_session_created_at", table_name="chat_context_events")
    op.drop_table("chat_context_events")

    op.drop_constraint("recovery_status_allowed", "chat_sessions", type_="check")
    op.drop_constraint("state_version_non_negative", "chat_sessions", type_="check")
    op.drop_column("chat_sessions", "recovery_status")
    op.drop_column("chat_sessions", "artifact_heads")
    op.drop_column("chat_sessions", "context_state")
    op.drop_column("chat_sessions", "topic_id")
    op.drop_column("chat_sessions", "state_version")

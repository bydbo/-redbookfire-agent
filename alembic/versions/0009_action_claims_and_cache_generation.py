"""上下文修复 v2：认领预期信息、缓存条目代次与动作认领表（E7CTX-06/07/11，数据契约 §3.4）。

变更内容：

1. `chat_artifact_versions` 增加 `expected_head_version_id` / `expected_state_version` /
   `source_message_id`——认领时固化并发预期，提交时比较**当前 head 与生成前 head**，
   允许主动从旧版分支，同时阻止覆盖生成期间出现的新版本；NULL 仅兼容旧行，
   不赋予旧 pending 继续提交的资格；
2. `chat_image_parse_cache` 增加 `generation_id`——同一 cache_key 重建后生成新代次，
   事件与消费按它区分历次结果（旧行由 server_default 逐行回填身份）；
3. 新表 `chat_context_actions`：上下文用例动作认领与重放（动作幂等、恢复分页、
   线索编辑 / 选择共用），`(session_id, action_id)` 唯一、会话级联删除。

**downgrade 只回滚 schema**：动作认领与重放记录随表删除，不可恢复；
真实数据回滚必须另做备份并单独授权（与既有迁移同一口径）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- chat_artifact_versions：认领并发预期 ----
    op.add_column("chat_artifact_versions",
                  sa.Column("expected_head_version_id", sa.Uuid(), nullable=True))
    op.add_column("chat_artifact_versions",
                  sa.Column("expected_state_version", sa.Integer(), nullable=True))
    op.add_column("chat_artifact_versions",
                  sa.Column("source_message_id", sa.Uuid(), nullable=True))
    op.create_check_constraint(
        "expected_state_non_negative", "chat_artifact_versions",
        sa.text("expected_state_version IS NULL OR expected_state_version >= 0"),
    )

    # ---- chat_image_parse_cache：条目代次（旧行逐行回填身份） ----
    op.add_column(
        "chat_image_parse_cache",
        sa.Column("generation_id", sa.Uuid(),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
    )
    op.create_index("ix_chat_image_parse_cache_generation_id", "chat_image_parse_cache",
                    ["generation_id"])

    # ---- chat_messages：结构化操作绑定（v2 §3.3B，随来源用户消息持久保存）----
    op.add_column(
        "chat_messages",
        sa.Column("operation_binding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # ---- context_observations：事件类型扩展（v2 §3.7）----
    op.drop_constraint("event_type_allowed", "context_observations", type_="check")
    op.create_check_constraint(
        "event_type_allowed",
        "context_observations",
        sa.text(
            "event_type IN ('context_state_decision', 'context_assembled', "
            "'artifact_version_committed', 'cache_request_started', "
            "'cache_lookup_finished', 'cache_entry_created', "
            "'cache_entry_invalidated', 'cache_entry_consumed', "
            "'artifact_consumed', 'model_attempt_started', 'model_attempt_finished')"
        ),
    )

    # ---- chat_context_actions：动作认领与重放 ----
    op.create_table(
        "chat_context_actions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("action_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("expected_state_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("recovery_id", sa.Uuid(), nullable=True),
        sa.Column("retry_of", sa.Uuid(), nullable=True),
        sa.Column("accounting_message_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "action_id",
                            name="uq_chat_context_actions_session_action"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')",
            name="status_allowed",
        ),
    )
    op.create_index("ix_chat_context_actions_session_created_at", "chat_context_actions",
                    ["session_id", "created_at"])


def downgrade() -> None:
    # 只回滚 schema：动作认领 / 重放记录随表删除，不可恢复
    op.drop_index("ix_chat_context_actions_session_created_at", table_name="chat_context_actions")
    op.drop_table("chat_context_actions")

    op.drop_column("chat_messages", "operation_binding")

    op.drop_constraint("event_type_allowed", "context_observations", type_="check")
    op.create_check_constraint(
        "event_type_allowed",
        "context_observations",
        sa.text(
            "event_type IN ('context_state_decision', 'context_assembled', "
            "'artifact_version_committed', 'cache_lookup_finished', "
            "'artifact_consumed', 'model_attempt_finished')"
        ),
    )

    op.drop_index("ix_chat_image_parse_cache_generation_id",
                  table_name="chat_image_parse_cache")
    op.drop_column("chat_image_parse_cache", "generation_id")

    op.drop_constraint("expected_state_non_negative", "chat_artifact_versions", type_="check")
    op.drop_column("chat_artifact_versions", "source_message_id")
    op.drop_column("chat_artifact_versions", "expected_state_version")
    op.drop_column("chat_artifact_versions", "expected_head_version_id")

"""上下文修复 v2.1：审计事件类型补齐（A02，数据契约 §3.8）。

变更内容：

1. `chat_context_events.event_type` 的 CHECK 扩展 `select_object` / `complete_recovery`。
   服务层 `services/context_state.py::_commit` 早已按真实动作写这两个事件，但 0008 建表时
   的约束没有放行——`select_object`（对象选择 / 线索选定）与 `complete_recovery`（完成恢复）
   会触发 `ck_chat_context_events_event_type_allowed`，导致整个业务事务回滚。
   约束名与语义保持不变：`(session_id, state_version)` 唯一仍是 CAS 序号。

2. 不改 0008 / 0009（旧迁移是历史事实，不能原地改 hash 或语义）；恢复进度
   （`context_state.recovery`）本来就存在 JSONB 快照里，本轮不需要新增列。

**downgrade 只回滚 schema**：若已有 `select_object` / `complete_recovery` 审计行，回退会
因约束不满足而失败——这是刻意的边界提示，不能把 schema 回退描述成数据完整恢复；真实数据
回退须先备份并单独授权。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 本轮合法事件类型（与 `db/models.py::ChatContextEvent` 同口径）
_ALLOWED = (
    "delta", "confirm", "reject", "retract", "replace", "reset_topic",
    "select_object", "recovery", "complete_recovery",
)
#: 0008 的旧集合（downgrade 用）
_LEGACY = ("delta", "confirm", "reject", "retract", "replace", "reset_topic", "recovery")


def _condition(items: tuple[str, ...]) -> sa.TextClause:
    values = ", ".join(f"'{item}'" for item in items)
    return sa.text(f"event_type IN ({values})")


def upgrade() -> None:
    op.drop_constraint("event_type_allowed", "chat_context_events", type_="check")
    op.create_check_constraint(
        "event_type_allowed", "chat_context_events", _condition(_ALLOWED)
    )


def downgrade() -> None:
    op.drop_constraint("event_type_allowed", "chat_context_events", type_="check")
    op.create_check_constraint(
        "event_type_allowed", "chat_context_events", _condition(_LEGACY)
    )

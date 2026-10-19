"""E6 身份迁移扩展结构；不自动改写业务数据。

已经执行合并的库禁止 downgrade 丢弃唯一归档，须使用停写前备份恢复。
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_identity_batches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("plan_digest", sa.Text(), nullable=False, unique=True),
        sa.Column("plan", JSONB(), nullable=False),
        sa.Column("counts", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("status = 'applied'", name="status_allowed"),
    )
    op.create_table(
        "material_identity_archive",
        sa.Column("batch_id", UUID(as_uuid=True),
                  sa.ForeignKey("material_identity_batches.id", ondelete="RESTRICT"),
                  primary_key=True),
        sa.Column("entity_type", sa.Text(), primary_key=True),
        sa.Column("original_key", sa.Text(), primary_key=True),
        sa.Column("record", JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False,
                  server_default=sa.text("'migration_capture'")),
        sa.CheckConstraint("provenance = 'migration_capture'", name="provenance_allowed"),
    )
    op.create_table(
        "material_id_aliases",
        sa.Column("old_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_id", UUID(as_uuid=True), nullable=False),
        sa.Column("batch_id", UUID(as_uuid=True),
                  sa.ForeignKey("material_identity_batches.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.CheckConstraint("old_id <> canonical_id", name="different_ids"),
    )
    op.create_index("ix_material_id_aliases_canonical_id", "material_id_aliases", ["canonical_id"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM material_identity_batches)" )).scalar():
        raise RuntimeError("身份合并已经应用；禁止丢弃归档，请从停写前备份恢复")
    op.drop_table("material_id_aliases")
    op.drop_table("material_identity_archive")
    op.drop_table("material_identity_batches")

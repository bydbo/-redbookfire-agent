"""E8 第一阶段：扩展历史快照字段与回填批次表（不改业务数据）。

先扩展列并对旧行保留 NULL / 空对象；回填由 `scripts/e8_snapshot.py plan/apply/verify`
显式执行。此 revision 不解除 `run_matches -> materials` 级联，避免结构与旧代码同时漂移。
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column(
        "execution_snapshot", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))
    op.add_column("runs", sa.Column(
        "evidence_meta", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))

    op.add_column("run_hotspots", sa.Column("raw_text", sa.Text(), nullable=True))
    op.add_column("run_hotspots", sa.Column("clue", postgresql.JSONB(), nullable=True))
    op.add_column("run_hotspots", sa.Column("gap_advice", postgresql.JSONB(), nullable=True))
    op.add_column("run_hotspots", sa.Column(
        "retrieval_meta", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))
    op.add_column("run_hotspots", sa.Column(
        "evidence_meta", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))

    op.add_column("run_matches", sa.Column(
        "material_snapshot", postgresql.JSONB(), nullable=True))
    op.add_column("run_matches", sa.Column(
        "evidence_meta", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))

    op.add_column("chat_artifact_versions", sa.Column(
        "evidence_snapshot", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'{}'::jsonb")))

    op.create_table(
        "e8_snapshot_batches",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("database", postgresql.JSONB(), nullable=False),
        sa.Column("schema_revision", sa.Text(), nullable=False),
        sa.Column("plan_digest", sa.Text(), nullable=False),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'applied'")),
        sa.Column("verification", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('applied', 'verified', 'failed')",
                           name="ck_e8_snapshot_batches_status_allowed"),
        sa.PrimaryKeyConstraint("id", name="pk_e8_snapshot_batches"),
        sa.UniqueConstraint("plan_digest", name="uq_e8_snapshot_batches_plan_digest"),
    )
    op.create_index("ix_e8_snapshot_batches_applied_at", "e8_snapshot_batches",
                    ["applied_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS(SELECT 1 FROM e8_snapshot_batches)")).scalar():
        raise RuntimeError("已应用 E8 快照批次，拒绝回退结构；请从停写前备份恢复")
    op.drop_index("ix_e8_snapshot_batches_applied_at", table_name="e8_snapshot_batches")
    op.drop_table("e8_snapshot_batches")
    op.drop_column("chat_artifact_versions", "evidence_snapshot")
    op.drop_column("run_matches", "evidence_meta")
    op.drop_column("run_matches", "material_snapshot")
    op.drop_column("run_hotspots", "evidence_meta")
    op.drop_column("run_hotspots", "retrieval_meta")
    op.drop_column("run_hotspots", "gap_advice")
    op.drop_column("run_hotspots", "clue")
    op.drop_column("run_hotspots", "raw_text")
    op.drop_column("runs", "evidence_meta")
    op.drop_column("runs", "execution_snapshot")

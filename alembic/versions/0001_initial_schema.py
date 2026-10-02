"""初始 schema：五张表、索引与约束（对应数据契约 §3 / §4 / §6）。"""

# 本迁移只建结构、不灌数据；建库顺序按数据契约 §6：
#   CREATE EXTENSION IF NOT EXISTS vector -> pg_trgm -> 5 张表 -> 索引与约束 -> HNSW 索引。
# 边界：downgrade 只删表与索引，**不** DROP EXTENSION（扩展是同库共享资源）；
# 扩展正常由 docker/postgres/init/01-extensions.sql 在容器初始化时预建，这里 IF NOT EXISTS 兜底。
#
# Revision ID: 0001
# Revises: （无，首个迁移）
# Create Date: 2026-10-02

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建扩展 -> 建表 -> 建索引与约束 -> 建 HNSW 索引。"""
    # 数据契约 §6：扩展先于一切（需要超级用户权限，正常由 compose 初始化脚本预建）
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "hotspots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("clue", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hotspots")),
    )
    op.create_index("ix_hotspots_created_at", "hotspots", ["created_at"], unique=False)

    op.create_table(
        "materials",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), server_default=sa.text("'video'"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("tags", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("elements", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'"), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'filename'"), nullable=False),
        sa.Column("duration_s", sa.Numeric(precision=8, scale=2), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("width", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("height", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("has_audio", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("keyframes", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'"), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1024), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("source IN ('sidecar', 'vision', 'filename', 'legacy')",
                           name=op.f("ck_materials_source_allowed")),
        sa.CheckConstraint("type IN ('video', 'image')",
                           name=op.f("ck_materials_type_allowed")),
        sa.CheckConstraint("height >= 0", name=op.f("ck_materials_height_non_negative")),
        sa.CheckConstraint("size_bytes >= 0",
                           name=op.f("ck_materials_size_bytes_non_negative")),
        sa.CheckConstraint("width >= 0", name=op.f("ck_materials_width_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_materials")),
        sa.UniqueConstraint("path", name=op.f("uq_materials_path")),
    )
    op.create_index("ix_materials_fingerprint", "materials", ["fingerprint"], unique=False)
    op.create_index("ix_materials_indexed_at", "materials", ["indexed_at"], unique=False)
    op.create_index("ix_materials_tags", "materials", ["tags"], unique=False,
                    postgresql_using="gin")
    # 表达式索引：gin_trgm_ops 直接写进表达式（autogenerate 对表达式索引不可靠，已人工校对）
    op.create_index(
        "ix_materials_text_trgm", "materials",
        [sa.literal_column(
            "(coalesce(title, '') || ' ' || coalesce(description, '')) gin_trgm_ops")],
        unique=False, postgresql_using="gin")

    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llm_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_cny", sa.Numeric(precision=10, scale=4), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')",
                           name=op.f("ck_runs_status_allowed")),
        sa.CheckConstraint("cost_cny >= 0", name=op.f("ck_runs_cost_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("job_id", name=op.f("uq_runs_job_id")),
    )
    op.create_index("ix_runs_created_at", "runs", ["created_at"], unique=False)
    op.create_index("ix_runs_status", "runs", ["status"], unique=False)

    op.create_table(
        "run_hotspots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("hotspot_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("coverage", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'"), nullable=False),
        sa.Column("draft", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')",
                           name=op.f("ck_run_hotspots_status_allowed")),
        sa.CheckConstraint("position >= 1", name=op.f("ck_run_hotspots_position_positive")),
        sa.ForeignKeyConstraint(["hotspot_id"], ["hotspots.id"],
                                name=op.f("fk_run_hotspots_hotspot_id_hotspots"),
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"],
                                name=op.f("fk_run_hotspots_run_id_runs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_hotspots")),
        sa.UniqueConstraint("run_id", "position", name="uq_run_hotspots_run_id_position"),
    )
    op.create_index("ix_run_hotspots_hotspot_id", "run_hotspots", ["hotspot_id"], unique=False)

    op.create_table(
        "run_matches",
        sa.Column("run_hotspot_id", sa.UUID(), nullable=False),
        sa.Column("material_id", sa.UUID(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("recall_sources", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'"),
                  nullable=False),
        sa.Column("hits", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'"), nullable=False),
        sa.Column("missing", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'"), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'"), nullable=False),
        sa.Column("usage", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint("jsonb_array_length(reasons) >= 1",
                           name=op.f("ck_run_matches_reasons_not_empty")),
        sa.CheckConstraint("rank >= 1", name=op.f("ck_run_matches_rank_positive")),
        sa.CheckConstraint("score >= 0 AND score <= 1",
                           name=op.f("ck_run_matches_score_in_range")),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"],
                                name=op.f("fk_run_matches_material_id_materials"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_hotspot_id"], ["run_hotspots.id"],
                                name=op.f("fk_run_matches_run_hotspot_id_run_hotspots"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_hotspot_id", "material_id", name=op.f("pk_run_matches")),
        sa.UniqueConstraint("run_hotspot_id", "rank", name="uq_run_matches_run_hotspot_id_rank"),
    )
    op.create_index("ix_run_matches_material_id", "run_matches", ["material_id"], unique=False)

    # HNSW 放最后：它是这批索引里最贵的一个（数据契约 §6 的顺序）
    op.create_index("ix_materials_embedding_hnsw", "materials", ["embedding"], unique=False,
                    postgresql_using="hnsw",
                    postgresql_with={"m": 16, "ef_construction": 64},
                    postgresql_ops={"embedding": "vector_cosine_ops"})


def downgrade() -> None:
    """删表与索引；**不删扩展**（契约 §6：扩展是同库共享资源）。"""
    op.drop_index("ix_run_matches_material_id", table_name="run_matches")
    op.drop_table("run_matches")
    op.drop_index("ix_run_hotspots_hotspot_id", table_name="run_hotspots")
    op.drop_table("run_hotspots")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_created_at", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_materials_embedding_hnsw", table_name="materials",
                  postgresql_using="hnsw")
    op.drop_index("ix_materials_text_trgm", table_name="materials", postgresql_using="gin")
    op.drop_index("ix_materials_tags", table_name="materials", postgresql_using="gin")
    op.drop_index("ix_materials_indexed_at", table_name="materials")
    op.drop_index("ix_materials_fingerprint", table_name="materials")
    op.drop_table("materials")
    op.drop_index("ix_hotspots_created_at", table_name="hotspots")
    op.drop_table("hotspots")

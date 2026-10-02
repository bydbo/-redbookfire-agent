"""数据契约的五张表（SQLAlchemy 2.0 声明式映射）。

用途：把 `docs/contracts/数据契约.md` §3 的字段表、§4 的索引与约束、级联规则逐项落成 ORM。
输入：无（模型定义；写入由仓储层负责，见 S2.4）。
输出：`Material` / `Hotspot` / `Run` / `RunHotspot` / `RunMatch` 五个映射类。

口径提醒：
- `uuid` 主键默认值走 PostgreSQL 16 内置的 `gen_random_uuid()`，不需要 pgcrypto；
- `embedding` 用 `pgvector` 的 `Vector(1024)`，维度与数据契约 §3.1 一致（S2.2 的 DoR 已校准）；
- `run_matches.reasons` 除了 JSONB 之外还有 `jsonb_array_length(reasons) >= 1` 的 CHECK——
  与 `schemas.py` 模型层的非空约束呼应，契约要求两处都体现。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

EMBEDDING_DIM = 1024


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True,
                         server_default=text("gen_random_uuid()"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Material(Base):
    """`materials` — 素材索引（数据契约 §3.1）。"""

    __tablename__ = "materials"

    id: Mapped[uuid.UUID] = _pk()
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'video'"))
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False,
                                            server_default=text("'{}'"))
    elements: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    source: Mapped[str] = mapped_column(Text, nullable=False,
                                        server_default=text("'filename'"))
    duration_s: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False,
                                                server_default=text("0"))
    width: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    height: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    has_audio: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    keyframes: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("type IN ('video', 'image')", name="type_allowed"),
        CheckConstraint("source IN ('sidecar', 'vision', 'filename', 'legacy')",
                        name="source_allowed"),
        CheckConstraint("width >= 0", name="width_non_negative"),
        CheckConstraint("height >= 0", name="height_non_negative"),
        CheckConstraint("size_bytes >= 0", name="size_bytes_non_negative"),
        Index("ix_materials_fingerprint", "fingerprint"),
        Index("ix_materials_tags", "tags", postgresql_using="gin"),
        # 表达式索引：把 gin_trgm_ops 直接写进表达式（postgresql_ops 对 text() 表达式不生效）；
        # S2.3 生成迁移时这条需要人工校对（表达式索引 autogenerate 本来就不可靠）。
        Index("ix_materials_text_trgm",
              text("(coalesce(title, '') || ' ' || coalesce(description, '')) gin_trgm_ops"),
              postgresql_using="gin"),
        Index("ix_materials_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("ix_materials_indexed_at", "indexed_at"),
    )


class Hotspot(Base):
    """`hotspots` — 热点与线索（数据契约 §3.2）。"""

    __tablename__ = "hotspots"

    id: Mapped[uuid.UUID] = _pk()
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    clue: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_hotspots_created_at", "created_at"),)


class Run(Base):
    """`runs` — 一次运行的批次信息（数据契约 §3.3）。"""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False,
                                                   server_default=text("0"))
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False,
                                              server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')",
                        name="status_allowed"),
        CheckConstraint("cost_cny >= 0", name="cost_non_negative"),
        Index("ix_runs_status", "status"),
        Index("ix_runs_created_at", "created_at"),
    )


class RunHotspot(Base):
    """`run_hotspots` — 批次 × 热点（数据契约 §3.4）。"""

    __tablename__ = "run_hotspots"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    hotspot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hotspots.id", ondelete="RESTRICT"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    coverage: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    draft: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("position >= 1", name="position_positive"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')",
                        name="status_allowed"),
        UniqueConstraint("run_id", "position", name="uq_run_hotspots_run_id_position"),
        Index("ix_run_hotspots_hotspot_id", "hotspot_id"),
    )


class RunMatch(Base):
    """`run_matches` — 候选素材与命中依据（数据契约 §3.5）。"""

    __tablename__ = "run_matches"

    run_hotspot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("run_hotspots.id", ondelete="CASCADE"), primary_key=True)
    material_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("materials.id", ondelete="CASCADE"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    recall_sources: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False,
                                                      server_default=text("'{}'"))
    hits: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    missing: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    usage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    __table_args__ = (
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_in_range"),
        CheckConstraint("jsonb_array_length(reasons) >= 1", name="reasons_not_empty"),
        UniqueConstraint("run_hotspot_id", "rank", name="uq_run_matches_run_hotspot_id_rank"),
        Index("ix_run_matches_material_id", "material_id"),
    )

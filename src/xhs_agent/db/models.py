"""数据契约的七张表（SQLAlchemy 2.0 声明式映射）。

用途：把 `docs/contracts/数据契约.md` §3 的字段表、§4 的索引与约束、级联规则逐项落成 ORM。
输入：无（模型定义；写入由仓储层负责，见 S2.4 / S7.2）。
输出：`Material` / `Hotspot` / `Run` / `RunHotspot` / `RunMatch` / `ChatSession` /
      `ChatMessage` 七个映射类。

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
from typing import Any

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
    elements: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False,
                                                            server_default=text("'[]'"))
    source: Mapped[str] = mapped_column(Text, nullable=False,
                                        server_default=text("'filename'"))
    duration_s: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False,
                                                server_default=text("0"))
    width: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    height: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    has_audio: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    mtime: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    keyframes: Mapped[list[str]] = mapped_column(JSONB, nullable=False,
                                                  server_default=text("'[]'"))
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "path <> '' AND path !~ '(^/|:|\\\\|(^|/)[.]{1,2}(/|$)|//|/$|[[:cntrl:]]"
            "|(^|/)[[:space:]]|[[:space:]]($|/)|\\x7f)'",
            name="path_canonical"),
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


class MaterialIdentityBatch(Base):
    """身份迁移的已提交批次；完整计划用于核验和 E8 交接。"""

    __tablename__ = "material_identity_batches"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_digest: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    __table_args__ = (CheckConstraint("status = 'applied'", name="status_allowed"),)


class MaterialIdentityArchive(Base):
    """迁移时原记录，不能冒充运行时快照。"""

    __tablename__ = "material_identity_archive"
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("material_identity_batches.id", ondelete="RESTRICT"),
        primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text, primary_key=True)
    original_key: Mapped[str] = mapped_column(Text, primary_key=True)
    record: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(Text, nullable=False,
                                           server_default=text("'migration_capture'"))
    __table_args__ = (
        CheckConstraint("provenance = 'migration_capture'", name="provenance_allowed"),)


class MaterialIdAlias(Base):
    """旧 UUID → 规范 UUID；不与当前素材建立外键，删除后仍保留身份记录。"""

    __tablename__ = "material_id_aliases"
    old_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    canonical_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("material_identity_batches.id", ondelete="RESTRICT"),
        nullable=False)
    __table_args__ = (
        CheckConstraint("old_id <> canonical_id", name="different_ids"),
        Index("ix_material_id_aliases_canonical_id", "canonical_id"),)


class Hotspot(Base):
    """`hotspots` — 热点与线索（数据契约 §3.2）。"""

    __tablename__ = "hotspots"

    id: Mapped[uuid.UUID] = _pk()
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    clue: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_hotspots_created_at", "created_at"),)


class Run(Base):
    """`runs` — 一次运行的批次信息（数据契约 §3.3）。"""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    topk: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    prompt_versions: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False,
                                                            server_default=text("'{}'"))
    # S7.20：当前阶段快照（结构见 `core/progress.py` 与数据契约 §3.3）；只记阶段切换
    stage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                  server_default=text("'{}'"))
    # A05：提交时服务端筛选的适用事实快照（会话 / 话题 / 已验证产物），worker 生成阶段
    # 读取同一份；空对象表示本次没有可用约束（旧行 / 离线路径）。
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')",
                        name="status_allowed"),
        CheckConstraint("cost_cny >= 0", name="cost_non_negative"),
        CheckConstraint("topk >= 1 AND topk <= 20", name="topk_in_range"),
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
    coverage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                      server_default=text("'{}'"))
    draft: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
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
    hits: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False,
                                                        server_default=text("'[]'"))
    missing: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False,
                                                           server_default=text("'[]'"))
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False,
                                               server_default=text("'[]'"))
    usage: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))

    __table_args__ = (
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint("score >= 0 AND score <= 1", name="score_in_range"),
        CheckConstraint("jsonb_array_length(reasons) >= 1", name="reasons_not_empty"),
        UniqueConstraint("run_hotspot_id", "rank", name="uq_run_matches_run_hotspot_id_rank"),
        Index("ix_run_matches_material_id", "material_id"),
    )


class ChatSession(Base):
    """`chat_sessions` — 对话会话（数据契约 §3.6，S7.2 / S7.24）。"""

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = _pk()
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()
    # ---- E7 第六批（S7.24）：结构化上下文状态 ----
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    topic_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    artifact_heads: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    recovery_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'complete'"))

    __table_args__ = (
        # 普通 B-tree 就够：Postgres 反向扫描它即可满足 `ORDER BY updated_at DESC`
        Index("ix_chat_sessions_updated_at", "updated_at"),
        CheckConstraint("state_version >= 0", name="state_version_non_negative"),
        CheckConstraint("recovery_status IN ('complete', 'partial')",
                        name="recovery_status_allowed"),
    )


class ChatMessage(Base):
    """`chat_messages` — 对话消息（数据契约 §3.7，S7.2）。

    工具过程不单独建行：一个 assistant 消息的 `tool_calls` 里带齐本轮调了什么、结果与耗时。
    `run_id` 刻意**不加外键**——run 被删（例如素材回收站那类维护动作）不该动到历史消息。
    `prompt_tokens` / `completion_tokens` 是本轮用量（supervisor + 工具内部调用 + 本轮分析），
    S7.10 起与 `cost_cny` 一起落库。S7.21 起 `turn_id` / `trace_id` / `progress` 让刷新后
    仍能定位"这一轮跑到哪、为什么失败、去哪个 trace 回看"。
    """

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'succeeded'"))
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'"))
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'"))
    prompt_versions: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # S7.21：本轮标识与 trace 关联（都不加外键）；阶段快照结构与 runs.stage 相同
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                     server_default=text("'{}'"))
    # v2 §3.3B：结构化操作绑定（对象 / 基础版本 / 预期状态版本），随来源用户消息持久保存
    operation_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False,
                                              server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False,
                                                   server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="role_allowed"),
        CheckConstraint("status IN ('running', 'succeeded', 'failed', 'interrupted')",
                        name="status_allowed"),
        CheckConstraint("cost_cny >= 0", name="cost_non_negative"),
        CheckConstraint("latency_ms >= 0", name="latency_non_negative"),
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_non_negative"),
        Index("ix_chat_messages_session_id_created_at", "session_id", "created_at"),
    )


class ChatContextEvent(Base):
    """`chat_context_events` — 上下文状态审计（数据契约 §3.8，S7.24）。

    每次**被接受**的状态变更写一行，`state_version` 就是变更后的会话版本；同一会话内唯一，
    重复提交同一版本不会再次推进（服务端先做 CAS，再写审计，最后提交）。
    """

    __tablename__ = "chat_context_events"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'"))
    delta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("session_id", "state_version",
                         name="uq_chat_context_events_session_version"),
        CheckConstraint("state_version >= 0", name="state_version_non_negative"),
        CheckConstraint(
            "event_type IN ('delta', 'confirm', 'reject', 'retract', 'replace', "
            "'reset_topic', 'select_object', 'recovery', 'complete_recovery')",
            name="event_type_allowed",
        ),
        CheckConstraint("actor IN ('extract', 'user', 'system')", name="actor_allowed"),
        Index("ix_chat_context_events_session_created_at", "session_id", "created_at"),
    )


class ChatArtifactVersion(Base):
    """`chat_artifact_versions` — 文案 / 线索版本（数据契约 §3.9，S7.25）。

    请求认领行先提交（`pending`），成功后**同一事务**写 `version_no` / `content` 并切换会话
    `artifact_heads`；失败 / 拒绝 / 中断只更新终态，不占成功版本号。
    """

    __tablename__ = "chat_artifact_versions"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    hotspot_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_key: Mapped[str] = mapped_column(Text, nullable=False)
    # E7CTX-07（v2 修复）：认领时固化的并发预期——提交时比较**当前 head 与生成前 head**，
    # 不要求选定基础必须等于 head（允许主动从旧版分支，同时阻止覆盖生成期间的新版本）。
    expected_head_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    expected_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    version_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    prompt_versions: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("session_id", "request_key",
                         name="uq_chat_artifact_versions_session_request"),
        # 只有成功版本占号：失败 / 拒绝 / 中断记录不参与唯一性（部分唯一索引）
        Index("uq_chat_artifact_versions_object_version", "session_id", "object_key",
              "version_no", unique=True, postgresql_where=text("status = 'succeeded'")),
        Index("ix_chat_artifact_versions_object_version", "session_id", "object_key",
              "version_no"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'refused', 'interrupted')",
            name="status_allowed",
        ),
        CheckConstraint("object_type IN ('draft', 'clue')", name="object_type_allowed"),
        CheckConstraint("version_no IS NULL OR version_no >= 1", name="version_no_positive"),
        CheckConstraint(
            "(status = 'succeeded') = (version_no IS NOT NULL)",
            name="succeeded_has_version",
        ),
        CheckConstraint("base_version_id IS NULL OR base_version_id <> id",
                        name="base_not_self"),
        CheckConstraint("hotspot_position IS NULL OR hotspot_position >= 1",
                        name="position_positive"),
    )


class ChatImageParseCache(Base):
    """`chat_image_parse_cache` — 图片解析持久缓存（数据契约 §3.10，S7.27）。

    **缓存对象，不是业务产物**：到期 / 删除只影响复用，不影响 `chat_artifact_versions` 里
    用户确认或编辑过的线索版本。
    """

    __tablename__ = "chat_image_parse_cache"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    # E7CTX-11（v2 修复）：条目代次——同一 cache_key 重建后生成新值，事件与消费按它区分历次结果
    generation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    identity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "cache_key",
                         name="uq_chat_image_parse_cache_session_key"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_chat_image_parse_cache_expires_at", "expires_at"),
        Index("ix_chat_image_parse_cache_generation_id", "generation_id"),
    )


class ContextObservation(Base):
    """`context_observations` — 上下文与缓存度量事实源（数据契约 §3.11，S7.28）。

    旁路表：写入失败只告警，不阻断业务；`event_id` 是稳定 ID，重复投递按主键冲突忽略。
    """

    __tablename__ = "context_observations"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('context_state_decision', 'context_assembled', "
            "'artifact_version_committed', 'cache_request_started', "
            "'cache_lookup_finished', 'cache_entry_created', "
            "'cache_entry_invalidated', 'cache_entry_consumed', "
            "'artifact_consumed', 'model_attempt_started', 'model_attempt_finished')",
            name="event_type_allowed",
        ),
        CheckConstraint("attempt IS NULL OR attempt >= 1", name="attempt_positive"),
        Index("ix_context_observations_session_created_at", "session_id", "created_at"),
        Index("ix_context_observations_type_created_at", "event_type", "created_at"),
    )


class ChatContextAction(Base):
    """`chat_context_actions` — 上下文用例动作认领与重放（v2 修复，数据契约 §3.12）。

    `action_id` 是客户端生成的幂等键：同 (session, action_id) 且同请求体重放返回原结果，
    不同请求体返回 409；无操作也保存成功结果，但不推进状态、不伪造审计。
    恢复（recover / complete_recovery）、线索编辑与选择都复用本表，不新建通用任务平台。
    """

    __tablename__ = "chat_context_actions"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False)
    action_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    expected_state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                   server_default=text("'{}'"))
    error: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                  server_default=text("'{}'"))
    recovery_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    retry_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    accounting_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("session_id", "action_id",
                         name="uq_chat_context_actions_session_action"),
        CheckConstraint("status IN ('pending', 'running', 'succeeded', 'failed', 'interrupted')",
                        name="status_allowed"),
        Index("ix_chat_context_actions_session_created_at", "session_id", "created_at"),
    )

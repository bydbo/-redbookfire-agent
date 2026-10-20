"""E8 历史快照：来源元信息、素材摘要、执行配置与检索统计。

用途：为运行 / 热点 / 候选 / 改稿版本生成**不可变**证据对象；读侧只消费这些对象，
      不在 GET 或报告渲染时用当前素材、当前配置重算历史。
输入：ORM 行、`AppConfig`、检索结果 / 统计。
输出：可直接落 JSONB 的 dict；`digest` 为规范化 JSON 的 SHA-256。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..config import AppConfig
from ..db.models import Material as MaterialRow
from ..tools.retrieval import RetrievalParams

SCHEMA_VERSION = 1
ORIGINS = ("runtime", "legacy_run_record", "migration_capture",
           "cutover_capture", "missing")
COMPLETENESS = ("complete", "partial", "missing")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if is_dataclass(value):
        return asdict(value)  # type: ignore[arg-type]
    raise TypeError(f"无法规范化类型：{type(value).__name__}")


def canonical(value: Any) -> str:
    """确定性 JSON（键排序、无空格）；数值以十进制文本保留，避免 float 抖动。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_default)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def evidence_meta(
    *,
    origin: str,
    source_ref: str,
    completeness: str,
    payload: Any,
    captured_at: str | None = None,
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    """一个字段组的来源元信息；`origin` 与 `completeness` 独立。"""
    if origin not in ORIGINS:
        raise ValueError(f"未知证据来源：{origin}")
    if completeness not in COMPLETENESS:
        raise ValueError(f"未知完整度：{completeness}")
    return {
        "schema_version": SCHEMA_VERSION,
        "origin": origin,
        "captured_at": captured_at or utc_now_iso(),
        "source_ref": source_ref,
        "digest": digest(payload) if payload is not None else "",
        "completeness": completeness,
        "missing_fields": list(missing_fields or []),
    }


def missing_meta(source_ref: str, fields: list[str]) -> dict[str, Any]:
    return evidence_meta(origin="missing", source_ref=source_ref,
                         completeness="missing", payload=None,
                         missing_fields=fields)


def material_snapshot(row: MaterialRow) -> dict[str, Any]:
    """当前素材行 → 历史快照（完整字段；不复制媒体内容）。"""
    keyframes = list(row.keyframes or [])
    return {
        "id": str(row.id),
        "path": row.path,
        "type": row.type,
        "title": row.title or "",
        "description": row.description or "",
        "tags": list(row.tags or []),
        "elements": list(row.elements or []),
        "duration_s": float(row.duration_s or 0),
        "width": int(row.width or 0),
        "height": int(row.height or 0),
        "has_audio": bool(row.has_audio),
        "size_bytes": int(row.size_bytes or 0),
        "mtime": row.mtime.astimezone(UTC).isoformat() if row.mtime else None,
        "source": row.source,
        "keyframes": keyframes,
        "keyframe_digest": digest(keyframes),
        "indexed_at": row.indexed_at.astimezone(UTC).isoformat() if row.indexed_at else None,
        "fingerprint": row.fingerprint or "",
        "embedding_model": row.embedding_model,
    }


def execution_snapshot(
    cfg: AppConfig, *, topk: int, prompt_versions: Mapping[str, int] | None = None,
    context_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """执行期允许名单快照；不保存密钥、完整环境变量或带凭据的地址。"""
    params = RetrievalParams.from_config(cfg)
    retrieval = {field.name: getattr(params, field.name) for field in fields(params)}
    retrieval["topk"] = int(topk)
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": utc_now_iso(),
        "llm": {
            "provider": cfg.llm.provider,
            "model": cfg.llm.model,
            "temperature": cfg.llm.temperature,
            "max_tokens": cfg.llm.max_tokens,
            "timeout_s": cfg.llm.timeout_s,
            "max_retries": cfg.llm.max_retries,
        },
        "embedding": {
            "enabled": bool(cfg.embedding.enabled),
            "model": cfg.embedding.model,
            "dim": cfg.embedding.dim,
        },
        "retrieval": retrieval,
        "workspace": {"materials_dir": cfg.materials_dir()},
        "prompt_versions": {str(k): int(v) for k, v in (prompt_versions or {}).items()},
        "context_snapshot_digest": digest(dict(context_snapshot or {})),
    }


def execution_evidence(snapshot: Mapping[str, Any], source_ref: str) -> dict[str, Any]:
    return evidence_meta(origin="runtime", source_ref=source_ref,
                         completeness="complete", payload=dict(snapshot))


def score_text(value: Any) -> str:
    return format(Decimal(str(value)), "f")

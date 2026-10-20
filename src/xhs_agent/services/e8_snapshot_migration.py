"""E8 历史快照回填：显式 plan / apply / verify（不在 GET 中自动执行）。

规则：
- 旧运行记录（coverage / draft / 分数 / 理由 / 初稿）逐字段保留，标 `legacy_run_record`；
- 素材依据优先用**校验通过的 E6 归档**（`migration_capture`），否则从当前素材采集
  （`cutover_capture`）；都没有则 `missing`，不用当前值伪造历史；
- 共享热点线索只标 `cutover_capture`，不冒充原运行线索；缺口建议与检索元信息不重算；
- 计划确定性、含数据库身份 / 来源摘要 / 逐类计数；apply 在事务内重建计划比对，整体回滚。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    ChatArtifactVersion,
    E8SnapshotBatch,
    Hotspot,
    Material,
    Run,
    RunHotspot,
    RunMatch,
)
from .material_identity_migration import database_identity
from .material_identity_migration import digest as identity_digest
from .run_snapshots import canonical, digest, evidence_meta, utc_now_iso

RULE_VERSION = 1
LOCK_KEY = 728_001
TABLES = ("runs", "run_hotspots", "run_matches", "chat_artifact_versions", "materials",
          "hotspots", "material_identity_archive")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _json_safe(value: Any) -> Any:
    """把计划转成 JSON 原生值（Decimal 用十进制文本保留，供 JSONB / CLI）。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _material_record_snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
    """E6 归档 material 记录 → E8 素材快照（保留归档数值，不读当前行）。"""
    duration = record.get("duration_s")
    if isinstance(duration, Decimal):
        duration = format(duration, "f")
    keyframes = [str(item) for item in record.get("keyframes") or []]
    return {
        "id": str(record.get("id") or ""),
        "path": str(record.get("path") or ""),
        "type": str(record.get("type") or "video"),
        "title": str(record.get("title") or ""),
        "description": str(record.get("description") or ""),
        "tags": [str(item) for item in record.get("tags") or []],
        "elements": list(record.get("elements") or []),
        "duration_s": str(duration if duration is not None else "0"),
        "width": int(record.get("width") or 0),
        "height": int(record.get("height") or 0),
        "has_audio": bool(record.get("has_audio")),
        "size_bytes": int(record.get("size_bytes") or 0),
        "mtime": record.get("mtime"),
        "source": str(record.get("source") or "filename"),
        "keyframes": keyframes,
        "keyframe_digest": digest(keyframes),
        "indexed_at": record.get("indexed_at"),
        "fingerprint": str(record.get("fingerprint") or ""),
        "embedding_model": record.get("embedding_model"),
    }


def _snapshot_from_row(row: Material) -> dict[str, Any]:
    keyframes = [str(item) for item in row.keyframes or []]
    return {
        "id": str(row.id),
        "path": row.path,
        "type": row.type,
        "title": row.title or "",
        "description": row.description or "",
        "tags": list(row.tags or []),
        "elements": list(row.elements or []),
        "duration_s": format(row.duration_s or Decimal("0"), "f"),
        "width": int(row.width or 0),
        "height": int(row.height or 0),
        "has_audio": bool(row.has_audio),
        "size_bytes": int(row.size_bytes or 0),
        "mtime": _iso(row.mtime),
        "source": row.source,
        "keyframes": keyframes,
        "keyframe_digest": digest(keyframes),
        "indexed_at": _iso(row.indexed_at),
        "fingerprint": row.fingerprint or "",
        "embedding_model": row.embedding_model,
    }


async def _e6_materials(session: AsyncSession) -> dict[str, tuple[dict[str, Any], str]]:
    """E6 归档中的 materials（逐条先用归档自带 digest 校验，坏记录不参与回填）。"""
    rows = (await session.execute(text(
        "SELECT original_key, record::text AS record, digest FROM material_identity_archive "
        "WHERE entity_type = 'materials' ORDER BY batch_id, original_key"))).mappings().all()
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for row in rows:
        record = json.loads(str(row["record"]), parse_float=Decimal)
        if identity_digest(record) != str(row["digest"]):
            raise ValueError(f"E6 归档摘要校验失败：materials/{row['original_key']}")
        valid = json.loads(str(row["record"]), parse_float=Decimal)
        out[str(row["original_key"])] = (valid, str(row["digest"]))
    return out


def _run_evidence(captured_at: str, run_id: uuid.UUID) -> dict[str, Any]:
    return {"execution": evidence_meta(
        origin="missing", source_ref=f"run:{run_id}/execution",
        captured_at=captured_at, completeness="missing", payload=None,
        missing_fields=["execution_snapshot"])}


def _hotspot_evidence(
    *, captured_at: str, run_id: uuid.UUID, position: int, raw: str | None,
    clue: dict[str, Any] | None, coverage: dict[str, Any], draft: dict[str, Any] | None,
) -> dict[str, Any]:
    source_ref = f"run:{run_id}/position:{position}"
    meta = {
        "raw_text": evidence_meta(
            origin="cutover_capture" if raw is not None else "missing",
            source_ref=source_ref, captured_at=captured_at,
            completeness="complete" if raw is not None else "missing",
            payload=raw, missing_fields=[] if raw is not None else ["raw_text"]),
        "clue": evidence_meta(
            origin="cutover_capture" if clue else "missing", source_ref=source_ref,
            captured_at=captured_at, completeness="complete" if clue else "missing",
            payload=clue, missing_fields=[] if clue else ["clue"]),
        "coverage": evidence_meta(
            origin="legacy_run_record", source_ref=source_ref, captured_at=captured_at,
            completeness="complete" if coverage else "partial", payload=coverage),
        "draft": evidence_meta(
            origin="legacy_run_record" if draft else "missing", source_ref=source_ref,
            captured_at=captured_at, completeness="complete" if draft else "missing",
            payload=draft, missing_fields=[] if draft else ["draft"]),
        "gap_advice": evidence_meta(
            origin="missing", source_ref=source_ref, captured_at=captured_at,
            completeness="missing", payload=None, missing_fields=["gap_advice"]),
        "retrieval": evidence_meta(
            origin="missing", source_ref=source_ref, captured_at=captured_at,
            completeness="missing", payload=None, missing_fields=["retrieval_meta"]),
    }
    return meta


def _artifact_evidence(
    *, captured_at: str, run_id: str | None, position: int | None,
    content: dict[str, Any] | None, clue: dict[str, Any] | None,
    material: dict[str, Any] | None, clue_origin: str, material_origin: str,
) -> dict[str, Any]:
    missing = []
    if not clue:
        missing.append("clue")
    if not material:
        missing.append("material")
    if not content:
        missing.append("content")
    source_ref = f"run:{run_id}/position:{position}"
    if missing:
        return {"schema_version": 1, "origin": "missing", "captured_at": captured_at,
                "source_ref": source_ref, "digest": "",
                "completeness": "missing", "missing_fields": missing,
                "clue": None, "material": None, "base_draft": content}
    origin = ("runtime" if clue_origin == material_origin == "runtime"
              else "legacy_run_record")
    return {
        "schema_version": 1, "origin": origin, "captured_at": captured_at,
        "source_ref": source_ref,
        "digest": digest({"clue": clue, "material": material, "base_draft": content}),
        "completeness": "complete", "missing_fields": [],
        "clue": clue, "material": material, "base_draft": content,
        "sources": {
            "clue": evidence_meta(origin=clue_origin, source_ref=source_ref,
                                  captured_at=captured_at, completeness="complete",
                                  payload=clue),
            "material": evidence_meta(origin=material_origin, source_ref=source_ref,
                                      captured_at=captured_at, completeness="complete",
                                      payload=material),
        },
    }


async def build_plan(
    session: AsyncSession, *, code_version: str, identity: dict[str, str],
    captured_at: str | None = None,
) -> dict[str, Any]:
    """确定性回填计划：只读，不改业务数据。"""
    captured_at = captured_at or utc_now_iso()
    revision = str((await session.execute(text(
        "SELECT version_num FROM alembic_version"))).scalar_one())
    e6 = await _e6_materials(session)
    runs = (await session.execute(select(Run).order_by(Run.id))).scalars().all()
    hotspots = (
        await session.execute(select(RunHotspot).order_by(RunHotspot.run_id, RunHotspot.position))
    ).scalars().all()
    matches = (
        await session.execute(select(RunMatch).order_by(
            RunMatch.run_hotspot_id, RunMatch.rank))
    ).scalars().all()
    hotspot_ids = [row.hotspot_id for row in hotspots]
    shared = {
        row.id: row for row in (
            await session.execute(select(Hotspot).where(Hotspot.id.in_(hotspot_ids)))
        ).scalars()
    } if hotspot_ids else {}
    current_materials = {
        mat.id: mat for mat in (
            await session.execute(select(Material).where(
                Material.id.in_([match.material_id for match in matches])))
        ).scalars()
    } if matches else {}

    run_targets = []
    for run in runs:
        existing_evidence = dict(run.evidence_meta or {})
        if run.execution_snapshot and existing_evidence.get("execution"):
            execution_evidence = existing_evidence["execution"]
        else:
            execution_evidence = _run_evidence(captured_at, run.id)["execution"]
        run_targets.append({
            "id": str(run.id),
            "execution_snapshot": dict(run.execution_snapshot or {}),
            "evidence_meta": {"execution": execution_evidence},
        })
    hotspot_targets = []
    for row in hotspots:
        existing = dict(row.evidence_meta or {})
        if (existing.get("clue") or {}).get("origin") == "runtime" and row.clue:
            hotspot_targets.append({
                "id": str(row.id), "raw_text": row.raw_text, "clue": dict(row.clue),
                "gap_advice": dict(row.gap_advice or {}) or None,
                "retrieval_meta": dict(row.retrieval_meta or {}),
                "evidence_meta": existing,
            })
            continue
        raw = row.raw_text
        if raw is None:
            hotspot = shared.get(row.hotspot_id)
            raw = hotspot.raw_text if hotspot is not None else None
        clue = dict(row.clue) if row.clue else None
        if clue is None:
            hotspot = shared.get(row.hotspot_id)
            if hotspot is not None and hotspot.clue:
                clue = dict(hotspot.clue)
        hotspot_targets.append({
            "id": str(row.id), "raw_text": raw, "clue": clue,
            "gap_advice": dict(row.gap_advice or {}) or None,
            "retrieval_meta": dict(row.retrieval_meta or {}),
            "evidence_meta": _hotspot_evidence(
                captured_at=captured_at, run_id=row.run_id, position=row.position,
                raw=raw, clue=clue, coverage=dict(row.coverage or {}), draft=row.draft),
        })
    match_targets: list[dict[str, Any]] = []
    match_origins: dict[tuple[str, str], str] = {}
    stats = {"materials_migration_capture": 0, "materials_cutover_capture": 0,
             "materials_missing": 0, "artifacts_complete": 0, "artifacts_missing": 0}
    for match in matches:
        existing_candidate = (dict(match.evidence_meta or {}).get("candidate") or {})
        if (match.material_snapshot is not None
                and existing_candidate.get("origin") == "runtime"):
            match_targets.append({
                "run_hotspot_id": str(match.run_hotspot_id),
                "material_id": str(match.material_id),
                "material_snapshot": dict(match.material_snapshot),
                "evidence_meta": dict(match.evidence_meta or {}),
            })
            match_origins[(str(match.run_hotspot_id), str(match.material_id))] = "runtime"
            continue
        archived = e6.get(str(match.material_id))
        if archived is not None:
            snapshot = _material_record_snapshot(archived[0])
            origin, source_ref = "migration_capture", f"e6:{archived[1]}"
            stats["materials_migration_capture"] += 1
        elif match.material_id in current_materials:
            snapshot = _snapshot_from_row(current_materials[match.material_id])
            origin, source_ref = "cutover_capture", f"material:{match.material_id}"
            stats["materials_cutover_capture"] += 1
        else:
            snapshot, origin, source_ref = None, "missing", f"material:{match.material_id}"
            stats["materials_missing"] += 1
        meta = evidence_meta(
            origin=origin, source_ref=source_ref, captured_at=captured_at,
            completeness="complete" if snapshot else "missing", payload=snapshot,
            missing_fields=[] if snapshot else ["material_snapshot"])
        match_origins[(str(match.run_hotspot_id), str(match.material_id))] = origin
        match_targets.append({
            "run_hotspot_id": str(match.run_hotspot_id),
            "material_id": str(match.material_id),
            "material_snapshot": snapshot,
            "evidence_meta": {"candidate": {**meta, "origin": origin}},
        })

    planned_matches: dict[tuple[str, str], dict[str, Any] | None] = {
        (item["run_hotspot_id"], item["material_id"]):
            (item["material_snapshot"] if item["material_snapshot"] is None
             else dict(item["material_snapshot"]))
        for item in match_targets
    }
    artifact_targets = []
    artifacts = (
        await session.execute(select(ChatArtifactVersion).order_by(ChatArtifactVersion.id))
    ).scalars().all()
    for artifact in artifacts:
        if (artifact.evidence_snapshot
                and (artifact.evidence_snapshot or {}).get("origin") == "runtime"):
            artifact_targets.append({
                "id": str(artifact.id),
                "evidence_snapshot": dict(artifact.evidence_snapshot),
            })
            stats["artifacts_complete"] += 1
            continue
        if artifact.object_type != "draft" or not artifact.run_id:
            artifact_targets.append({
                "id": str(artifact.id),
                "evidence_snapshot": evidence_meta(
                    origin="missing", source_ref=f"artifact:{artifact.id}",
                    captured_at=captured_at, completeness="missing", payload=None,
                    missing_fields=["run_id"]),
            })
            stats["artifacts_missing"] += 1
            continue
        run_hotspot = next(
            (hs for hs in hotspots
             if hs.run_id == artifact.run_id
             and hs.position == artifact.hotspot_position), None)
        material_id = ""
        if isinstance(artifact.content, dict):
            material_id = str(artifact.content.get("material_id") or "")
        artifact_match = next((item for item in matches
                               if run_hotspot is not None
                               and item.run_hotspot_id == run_hotspot.id
                               and str(item.material_id) == material_id), None)
        material: dict[str, Any] | None = None
        material_origin = "missing"
        if artifact_match is not None:
            planned = planned_matches.get(
                (str(artifact_match.run_hotspot_id), str(artifact_match.material_id)))
            material = dict(planned) if planned else None
            material_origin = match_origins.get(
                (str(artifact_match.run_hotspot_id), str(artifact_match.material_id)),
                "missing")
        clue = dict(run_hotspot.clue) if run_hotspot is not None and run_hotspot.clue else None
        if clue is None and run_hotspot is not None:
            hotspot_row = shared.get(run_hotspot.hotspot_id)
            if hotspot_row is not None and hotspot_row.clue:
                clue = dict(hotspot_row.clue)
        clue_meta = ((run_hotspot.evidence_meta or {}).get("clue")
                     if run_hotspot is not None else None) or {}
        clue_origin = str(clue_meta.get("origin") or ("cutover_capture" if clue else "missing"))
        evidence = _artifact_evidence(
            captured_at=captured_at, run_id=str(artifact.run_id),
            position=artifact.hotspot_position,
            content=dict(artifact.content) if isinstance(artifact.content, dict) else None,
            clue=clue, material=material, clue_origin=clue_origin,
            material_origin=material_origin)
        stats["artifacts_complete" if evidence["completeness"] == "complete"
              else "artifacts_missing"] += 1
        artifact_targets.append({"id": str(artifact.id),
                                 "evidence_snapshot": evidence})

    counts = {
        "runs": len(run_targets), "run_hotspots": len(hotspot_targets),
        "run_matches": len(match_targets), "artifacts": len(artifact_targets), **stats,
    }
    source_digest = digest({
        "runs": [{
            "id": str(row.id), "topk": row.topk, "status": row.status,
            "prompt_versions": dict(row.prompt_versions or {}),
            "context_snapshot": dict(row.context_snapshot or {}),
            "execution_snapshot": dict(row.execution_snapshot or {}),
            "evidence_meta": dict(row.evidence_meta or {}),
        } for row in runs],
        "hotspots": [{
            "id": str(row.id), "run_id": str(row.run_id), "position": row.position,
            "status": row.status, "coverage": dict(row.coverage or {}),
            "draft": row.draft, "error": row.error, "raw_text": row.raw_text,
            "clue": row.clue, "gap_advice": row.gap_advice,
            "retrieval_meta": dict(row.retrieval_meta or {}),
            "evidence_meta": dict(row.evidence_meta or {}),
        } for row in hotspots],
        "matches": [{
            "rh": str(row.run_hotspot_id), "material": str(row.material_id),
            "rank": row.rank, "score": str(row.score),
            "reasons": list(row.reasons or []), "hits": list(row.hits or []),
            "missing": list(row.missing or []), "usage": row.usage,
            "material_snapshot": row.material_snapshot,
            "evidence_meta": dict(row.evidence_meta or {}),
        } for row in matches],
        "materials": [{
            "id": str(mat.id), "path": mat.path, "title": mat.title,
            "fingerprint": mat.fingerprint,
            "indexed_at": _iso(mat.indexed_at),
            "embedding_model": mat.embedding_model,
        } for mat in current_materials.values()],
        "artifacts": [{
            "id": str(art.id), "status": art.status, "content": art.content,
            "evidence_snapshot": dict(art.evidence_snapshot or {}),
        } for art in artifacts],
    })
    plan: dict[str, Any] = {
        "rule_version": RULE_VERSION, "code_version": code_version,
        "schema_revision": revision, "database": identity,
        "captured_at": captured_at, "counts": counts, "source_digest": source_digest,
        "targets": {
            "runs": run_targets, "run_hotspots": hotspot_targets,
            "run_matches": match_targets, "artifacts": artifact_targets,
        },
        "executable": bool(identity.get("cluster")) and
        identity.get("cluster_source") == "pg_control_system",
    }
    plan["targets"] = _json_safe(plan["targets"])
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    plan["plan_digest"] = digest(unsigned)
    plan["batch_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, plan["plan_digest"]))
    return plan


async def plan_snapshot(session: AsyncSession, *, code_version: str) -> dict[str, Any]:
    return await build_plan(session, code_version=code_version,
                            identity=await database_identity(session))


def _verify_plan_integrity(plan: Mapping[str, Any]) -> None:
    unsigned = {key: value for key, value in plan.items()
                if key not in {"plan_digest", "batch_id"}}
    if digest(unsigned) != plan.get("plan_digest"):
        raise ValueError("计划摘要不一致（内容被修改）")
    if str(plan.get("rule_version")) != str(RULE_VERSION):
        raise ValueError("规则版本不兼容")


async def apply_snapshot(
    session: AsyncSession, plan: Mapping[str, Any], *, code_version: str,
) -> dict[str, Any]:
    """事务内重建计划并核对，再按目标写入；已有批次只读返回。"""
    _verify_plan_integrity(plan)
    if not plan.get("executable"):
        raise ValueError("计划不可执行：缺少可读的数据库集群身份")
    batch_id = uuid.UUID(str(plan["batch_id"]))
    async with session.begin():
        if await database_identity(session) != plan["database"]:
            raise ValueError("计划不属于当前数据库")
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                              {"key": LOCK_KEY})
        existing = await session.get(E8SnapshotBatch, batch_id)
        if existing is not None:
            await verify_snapshot(session, batch_id)
            return {"batch_id": str(batch_id), "status": "already_applied",
                    **dict(existing.counts or {})}
        rebuilt = await build_plan(session, code_version=code_version,
                                   identity=plan["database"],
                                   captured_at=str(plan["captured_at"]))
        if rebuilt["targets"] != plan["targets"] or rebuilt["source_digest"] != plan["source_digest"]:
            raise ValueError("计划已过期或与源数据不符，请重新 plan")
        for target in plan["targets"]["runs"]:
            await session.execute(text(
                "UPDATE runs SET execution_snapshot = CAST(:snapshot AS jsonb), "
                "evidence_meta = CAST(:evidence AS jsonb) WHERE id = :id"),
                {"id": target["id"], "snapshot": canonical(target["execution_snapshot"]),
                 "evidence": canonical(target["evidence_meta"])})
        for target in plan["targets"]["run_hotspots"]:
            await session.execute(text(
                "UPDATE run_hotspots SET raw_text = :raw, clue = CAST(:clue AS jsonb), "
                "gap_advice = CAST(:gap AS jsonb), retrieval_meta = CAST(:retrieval AS jsonb), "
                "evidence_meta = CAST(:evidence AS jsonb) WHERE id = :id"),
                {"id": target["id"], "raw": target["raw_text"],
                 "clue": canonical(target["clue"]) if target["clue"] is not None else None,
                 "gap": canonical(target["gap_advice"]) if target["gap_advice"] is not None else None,
                 "retrieval": canonical(target["retrieval_meta"]),
                 "evidence": canonical(target["evidence_meta"])})
        for target in plan["targets"]["run_matches"]:
            await session.execute(text(
                "UPDATE run_matches SET material_snapshot = CAST(:snapshot AS jsonb), "
                "evidence_meta = CAST(:evidence AS jsonb) "
                "WHERE run_hotspot_id = :rh AND material_id = :mid"),
                {"rh": target["run_hotspot_id"], "mid": target["material_id"],
                 "snapshot": canonical(target["material_snapshot"])
                 if target["material_snapshot"] is not None else None,
                 "evidence": canonical(target["evidence_meta"])})
        for target in plan["targets"]["artifacts"]:
            await session.execute(text(
                "UPDATE chat_artifact_versions SET evidence_snapshot = CAST(:evidence AS jsonb) "
                "WHERE id = :id"),
                {"id": target["id"], "evidence": canonical(target["evidence_snapshot"])})
        session.add(E8SnapshotBatch(
            id=batch_id, rule_version=RULE_VERSION, code_version=code_version,
            database=plan["database"], schema_revision=plan["schema_revision"],
            plan_digest=plan["plan_digest"], source_digest=plan["source_digest"],
            plan=dict(plan), counts=dict(plan["counts"]), status="applied"))
        await session.flush()
        result = await verify_snapshot(session, batch_id, commit=False)
    return result


async def verify_snapshot(session: AsyncSession, batch_id: uuid.UUID, *,
                          commit: bool = True) -> dict[str, Any]:
    batch = await session.get(E8SnapshotBatch, batch_id)
    if batch is None:
        raise ValueError("E8 快照批次不存在")
    plan = dict(batch.plan or {})
    _verify_plan_integrity(plan)
    if str(batch.plan_digest) != str(plan["plan_digest"]):
        raise ValueError("批次计划摘要不一致")
    for target in plan["targets"]["runs"]:
        row = (await session.execute(text(
            "SELECT execution_snapshot::text AS s, evidence_meta::text AS e "
            "FROM runs WHERE id=:id"), {"id": target["id"]})).mappings().one_or_none()
        if (row is None or json.loads(row["s"]) != target["execution_snapshot"]
                or json.loads(row["e"]) != target["evidence_meta"]):
            raise ValueError("runs 快照核验失败")
    for target in plan["targets"]["run_hotspots"]:
        row = (await session.execute(text(
            "SELECT raw_text, clue::text AS clue, gap_advice::text AS gap, "
            "retrieval_meta::text AS retrieval, evidence_meta::text AS evidence "
            "FROM run_hotspots WHERE id=:id"), {"id": target["id"]})).mappings().one_or_none()
        if row is None:
            raise ValueError("run_hotspots 快照核验失败")
        expected = target
        actual = {
            "raw_text": row["raw_text"],
            "clue": json.loads(row["clue"]) if row["clue"] is not None else None,
            "gap_advice": json.loads(row["gap"]) if row["gap"] is not None else None,
            "retrieval_meta": json.loads(row["retrieval"]),
            "evidence_meta": json.loads(row["evidence"]),
        }
        if actual != {key: expected[key] for key in actual}:
            raise ValueError("run_hotspots 快照核验失败")
    for target in plan["targets"]["run_matches"]:
        row = (await session.execute(text(
            "SELECT material_snapshot::text AS s FROM run_matches "
            "WHERE run_hotspot_id=:rh AND material_id=:mid"),
            {"rh": target["run_hotspot_id"], "mid": target["material_id"]})).scalar_one_or_none()
        expected = target["material_snapshot"]
        actual_snapshot = json.loads(row) if row is not None else None
        if actual_snapshot != expected:
            raise ValueError("run_matches 素材快照核验失败")
    for target in plan["targets"]["artifacts"]:
        row = (await session.execute(text(
            "SELECT evidence_snapshot::text AS e FROM chat_artifact_versions WHERE id=:id"),
            {"id": target["id"]})).scalar_one_or_none()
        if row is None or json.loads(row) != target["evidence_snapshot"]:
            raise ValueError("chat_artifact_versions 证据核验失败")
    actual_counts = (await session.execute(text(
        "SELECT (SELECT count(*) FROM runs) AS runs, "
        "(SELECT count(*) FROM run_hotspots) AS run_hotspots, "
        "(SELECT count(*) FROM run_matches) AS run_matches, "
        "(SELECT count(*) FROM chat_artifact_versions) AS artifacts"))).mappings().one()
    for key in ("runs", "run_hotspots", "run_matches", "artifacts"):
        if int(actual_counts[key]) != int(plan["counts"][key]):
            raise ValueError(f"E8 快照计数核验失败：{key}")
    batch.status = "verified"
    batch.verification = {"counts": dict(batch.counts), "verified": True}
    batch.verified_at = datetime.now(UTC)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return {"batch_id": str(batch_id), "status": "verified", **dict(batch.counts)}

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
    MaterialIdentityBatch,
    Run,
    RunHotspot,
    RunMatch,
)
from .material_identity_migration import _verify_archive as verify_identity_archive
from .material_identity_migration import _verify_plan_integrity as verify_identity_plan
from .material_identity_migration import database_identity, encoded
from .run_snapshots import (
    COMPLETENESS,
    ORIGINS,
    canonical,
    digest,
    evidence_meta,
    text_evidence_missing,
    utc_now_iso,
)

RULE_VERSION = 2
LOCK_KEY = 728_001
TABLES = ("runs", "run_hotspots", "run_matches", "chat_artifact_versions", "materials",
          "hotspots", "material_identity_archive", "material_identity_batches",
          "material_id_aliases", "alembic_version")

# Existing business values remain authoritative. Stage progress / updated_at are
# operational metadata and are not immutable evidence after service resumes.
PROTECTED_EXCLUDED = {
    "runs": ("execution_snapshot", "evidence_meta", "stage"),
    "run_hotspots": ("raw_text", "clue", "gap_advice", "retrieval_meta", "evidence_meta"),
    "run_matches": ("material_snapshot", "evidence_meta"),
    "chat_artifact_versions": ("evidence_snapshot", "updated_at"),
}


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
        "original_material_id": str(record.get("id") or ""),
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
        "keyframe_contents": [],  # E6 did not capture image bytes at run time.
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
        "keyframe_contents": [],  # Cutover files cannot prove the historical image version.
        "indexed_at": _iso(row.indexed_at),
        "fingerprint": row.fingerprint or "",
        "embedding_model": row.embedding_model,
    }


async def _e6_sources(session: AsyncSession) -> dict[str, Any]:
    """Use only batches whose plan, manifest and every archive record verify.

    Candidate decisions select the actual original material, which can differ
    from the canonical material chosen by E6. Multiple owners are an error.
    """
    batches = (await session.execute(select(MaterialIdentityBatch).order_by(
        MaterialIdentityBatch.created_at, MaterialIdentityBatch.id))).scalars().all()
    material_sources: dict[str, list[dict[str, Any]]] = {}
    decisions: dict[tuple[str, str], dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    source_refs = []
    for batch in batches:
        if batch.status not in {"applied", "verified"}:
            raise ValueError("E6 批次尚未成功应用，不能作为历史来源")
        verify_identity_plan(batch)
        await verify_identity_archive(session, batch)
        aliases.update({str(old): str(new) for old, new in batch.plan["aliases"].items()})
        rows = (await session.execute(text(
            "SELECT entity_type, original_key, record::text AS record, digest "
            "FROM material_identity_archive WHERE batch_id=:b ORDER BY entity_type,original_key"),
            {"b": batch.id})).mappings().all()
        archives = {(str(row["entity_type"]), str(row["original_key"])): {
            "record": json.loads(str(row["record"]), parse_float=Decimal),
            "ref": f"e6:{batch.id}/{row['entity_type']}/{row['original_key']}:{row['digest']}",
        } for row in rows}
        for (entity, key), archive in archives.items():
            if entity == "materials":
                if str(archive["record"].get("id")) != key:
                    raise ValueError("E6 素材归档身份与主键不一致")
                material_sources.setdefault(key, []).append(archive)
        for decision in batch.plan.get("candidate_decisions") or []:
            candidate_key = (str(decision["run_hotspot_id"]), str(decision["canonical_id"]))
            if candidate_key in decisions:
                raise ValueError("E6 候选归属不唯一，不能任选归档")
            original = str(decision["original_material_id"])
            candidate = archives.get(("run_matches", f"{candidate_key[0]}:{original}"))
            material = archives.get(("materials", original))
            if candidate is None or material is None:
                raise ValueError("E6 候选所需的原素材或候选归档缺失")
            if (str(candidate["record"].get("run_hotspot_id")) != candidate_key[0]
                    or str(candidate["record"].get("material_id")) != original):
                raise ValueError("E6 候选归档身份不一致")
            decisions[candidate_key] = {"candidate": candidate["record"], "material": material}
        source_refs.append({"batch_id": str(batch.id), "plan_digest": batch.plan_digest,
                            "manifest": batch.plan["archive_manifest"]})
    actual_aliases = {str(row.old_id): str(row.canonical_id) for row in (
        await session.execute(text("SELECT old_id,canonical_id FROM material_id_aliases"))).all()}
    if (actual_aliases != aliases or any(value in aliases or key == value
                                        for key, value in aliases.items())):
        raise ValueError("E6 别名与已验证批次不一致或存在链环")
    return {"materials": material_sources, "decisions": decisions, "sources": source_refs,
            "aliases": aliases}


async def _protected_records(session: AsyncSession) -> dict[str, list[dict[str, str]]]:
    """Lossless business records keyed by identity, without a second result store."""
    await session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    output: dict[str, list[dict[str, str]]] = {}
    for table, excluded in PROTECTED_EXCLUDED.items():
        # Identifiers / excluded columns are module constants, never user input.
        columns = ",".join(f"'{name}'" for name in excluded)
        rows = (await session.execute(text(
            f"SELECT (to_jsonb(t) - ARRAY[{columns}]::text[])::text FROM {table} t"
        ))).scalars().all()
        records = []
        for raw in rows:
            record = json.loads(raw, parse_float=Decimal)
            key = (f"{record['run_hotspot_id']}:{record['material_id']}"
                   if table == "run_matches" else str(record["id"]))
            records.append({"key": key, "record": encoded(record)})
        output[table] = sorted(records, key=lambda item: item["key"])
    return output


def _validate_existing_group(meta: Any, payload: Any, *, name: str) -> None:
    """A later plan must not re-sign corrupted already accepted evidence."""
    if (not isinstance(meta, dict) or meta.get("schema_version") != 1
            or meta.get("origin") not in ORIGINS
            or meta.get("completeness") not in COMPLETENESS
            or not meta.get("source_ref") or not meta.get("captured_at")
            or not isinstance(meta.get("missing_fields"), list)
            or (meta["completeness"] == "complete" and meta["missing_fields"])
            or (meta["origin"] == "missing" and meta["completeness"] != "missing")):
        raise ValueError(f"既有 {name} 证据元信息非法")
    expected = digest(payload) if payload is not None else ""
    accepted_digests = {expected}
    if meta["completeness"] == "missing" and payload in (None, {}, []):
        accepted_digests.add("")
    if meta.get("digest") not in accepted_digests:
        raise ValueError(f"既有 {name} 快照与证据摘要不一致")


def _validate_existing_artifact(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("completeness") == "missing":
        _validate_existing_group(snapshot, None, name="artifact")
        return
    payload = {key: snapshot.get(key) for key in ("clue", "material", "base_draft")}
    _validate_existing_group(snapshot, payload, name="artifact")
    if text_evidence_missing(payload["clue"], payload["material"], payload["base_draft"]):
        raise ValueError("既有 artifact 文字依据不完整")
    sources = snapshot.get("sources") or {}
    for key in ("clue", "material"):
        _validate_existing_group(sources.get(key), payload[key], name=f"artifact.sources.{key}")
        if (sources[key]["origin"] == "missing"
                or sources[key]["completeness"] != "complete"):
            raise ValueError("既有 artifact 来源不完整")


def _run_evidence(captured_at: str, run_id: uuid.UUID) -> dict[str, Any]:
    return {"execution": evidence_meta(
        origin="missing", source_ref=f"run:{run_id}/execution",
        captured_at=captured_at, completeness="missing", payload=None,
        missing_fields=["execution_snapshot"])}


def _hotspot_evidence(
    *, captured_at: str, run_id: uuid.UUID, position: int, raw: str | None,
    clue: dict[str, Any] | None, coverage: dict[str, Any], draft: dict[str, Any] | None,
    candidate_count: int,
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
        "candidate_integrity": evidence_meta(
            origin="legacy_run_record", source_ref=source_ref, captured_at=captured_at,
            completeness="partial", payload={"count": candidate_count},
            missing_fields=["deleted_candidates_unknown"]),
    }
    return meta


def _artifact_evidence(
    *, captured_at: str, run_id: str | None, position: int | None,
    content: dict[str, Any] | None, clue: dict[str, Any] | None,
    material: dict[str, Any] | None, clue_origin: str, material_origin: str,
    canonical_material_id: str = "",
) -> dict[str, Any]:
    missing = text_evidence_missing(clue, material, content)
    if clue_origin == "missing":
        missing.append("sources.clue")
    if material_origin == "missing":
        missing.append("sources.material")
    if (isinstance(content, dict) and isinstance(material, dict)
            and str(content.get("material_id") or "") != str(material.get("id") or "")):
        # E6 aliases prove canonical identity, not that two distinct original
        # descriptions were interchangeable when the draft was generated.
        missing.append("material_identity")
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
        "canonical_material_id": canonical_material_id,
        "draft_original_material_id": str((content or {}).get("material_id") or ""),
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
    await session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    # Existing batches are independent anchors for the original business values.
    # Rebuilding a new plan from today's rows must never bless an older damaged
    # score / reason simply because its material snapshot digest still matches.
    batches = (await session.execute(select(E8SnapshotBatch).order_by(
        E8SnapshotBatch.applied_at, E8SnapshotBatch.id))).scalars().all()
    for batch in batches:
        if batch.rule_version != RULE_VERSION:
            raise ValueError("既有 E8 旧规则批次缺少完整历史保护锚点，不能生成新的可执行计划")
        await verify_snapshot(session, batch.id, record_result=False)
    database_schema = str((await session.execute(text("SELECT current_schema()"))).scalar_one())
    e6 = await _e6_sources(session)
    protected_records = await _protected_records(session)
    protected_matches = {item["key"]: json.loads(item["record"], parse_float=Decimal)
                         for item in protected_records["run_matches"]}
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
        if existing_evidence.get("execution"):
            _validate_existing_group(existing_evidence["execution"], dict(run.execution_snapshot or {}),
                                     name="run.execution")
            execution_evidence = existing_evidence["execution"]
        else:
            execution_evidence = _run_evidence(captured_at, run.id)["execution"]
        run_targets.append({
            "id": str(run.id),
            "execution_snapshot": dict(run.execution_snapshot or {}),
            "evidence_meta": {**existing_evidence, "execution": execution_evidence},
        })
    hotspot_targets: list[dict[str, Any]] = []
    for row in hotspots:
        existing = dict(row.evidence_meta or {})
        candidate_count = sum(match.run_hotspot_id == row.id for match in matches)
        if existing.get("clue"):
            for name, payload in (("raw_text", row.raw_text), ("clue", row.clue),
                                  ("coverage", row.coverage), ("draft", row.draft),
                                  ("gap_advice", row.gap_advice), ("retrieval", row.retrieval_meta),
                                  ("candidate_integrity", {"count": candidate_count})):
                if name in existing:
                    _validate_existing_group(existing[name], payload, name=f"hotspot.{name}")
            existing.setdefault("candidate_integrity", evidence_meta(
                origin="legacy_run_record", source_ref=f"run:{row.run_id}/position:{row.position}",
                captured_at=captured_at, completeness="partial", payload={"count": candidate_count},
                missing_fields=["deleted_candidates_unknown"]))
            hotspot_targets.append({
                "id": str(row.id), "raw_text": row.raw_text,
                "clue": dict(row.clue) if row.clue is not None else None,
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
                raw=raw, clue=clue, coverage=dict(row.coverage or {}), draft=row.draft,
                candidate_count=candidate_count),
        })
    match_targets: list[dict[str, Any]] = []
    match_origins: dict[tuple[str, str], str] = {}
    stats = {"materials_migration_capture": 0, "materials_cutover_capture": 0,
             "materials_missing": 0, "artifacts_complete": 0, "artifacts_missing": 0}
    for match in matches:
        existing_candidate = (dict(match.evidence_meta or {}).get("candidate") or {})
        if existing_candidate:
            _validate_existing_group(existing_candidate, match.material_snapshot,
                                     name="candidate.material")
            match_targets.append({
                "run_hotspot_id": str(match.run_hotspot_id),
                "material_id": str(match.material_id),
                "material_snapshot": dict(match.material_snapshot)
                if match.material_snapshot is not None else None,
                "evidence_meta": dict(match.evidence_meta or {}),
            })
            preserved_origin = str(existing_candidate.get("origin") or "missing")
            match_origins[(str(match.run_hotspot_id), str(match.material_id))] = preserved_origin
            if preserved_origin in {"migration_capture", "cutover_capture", "missing"}:
                stats[f"materials_{preserved_origin}"] += 1
            continue
        decision = e6["decisions"].get((str(match.run_hotspot_id), str(match.material_id)))
        if decision is not None:
            current_record = protected_matches[f"{match.run_hotspot_id}:{match.material_id}"]
            if any(current_record.get(key) != decision["candidate"].get(key)
                   for key in ("rank", "score", "recall_sources", "hits", "missing", "reasons", "usage")):
                raise ValueError("E6 候选业务字段与归档不符，不能证明原素材归属")
            archived = decision["material"]
        else:
            sources = e6["materials"].get(str(match.material_id), [])
            if len(sources) > 1:
                raise ValueError("E6 素材归档来源不唯一，不能任选批次")
            archived = sources[0] if sources else None
        if archived is not None:
            snapshot = _material_record_snapshot(archived["record"])
            origin, source_ref = "migration_capture", archived["ref"]
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
        if artifact.evidence_snapshot:
            _validate_existing_artifact(artifact.evidence_snapshot)
            artifact_targets.append({
                "id": str(artifact.id),
                "evidence_snapshot": dict(artifact.evidence_snapshot),
            })
            stats["artifacts_complete" if artifact.evidence_snapshot.get("completeness") == "complete"
                  else "artifacts_missing"] += 1
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
        # Resolve only verified E6 aliases; never rewrite the version content.
        material_id = e6["aliases"].get(material_id, material_id)
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
        planned_hotspot = next((target for target in hotspot_targets
                                if run_hotspot is not None
                                and target["id"] == str(run_hotspot.id)), {})
        clue = dict(planned_hotspot["clue"]) if planned_hotspot.get("clue") else None
        clue_meta = (planned_hotspot.get("evidence_meta") or {}).get("clue") or {}
        clue_origin = str(clue_meta.get("origin") or ("cutover_capture" if clue else "missing"))
        evidence = _artifact_evidence(
            captured_at=captured_at, run_id=str(artifact.run_id),
            position=artifact.hotspot_position,
            content=dict(artifact.content) if isinstance(artifact.content, dict) else None,
            clue=clue, material=material, clue_origin=clue_origin,
            material_origin=material_origin, canonical_material_id=material_id)
        stats["artifacts_complete" if evidence["completeness"] == "complete"
              else "artifacts_missing"] += 1
        artifact_targets.append({"id": str(artifact.id),
                                 "evidence_snapshot": evidence})

    counts = {
        "runs": len(run_targets), "run_hotspots": len(hotspot_targets),
        "run_matches": len(match_targets), "artifacts": len(artifact_targets), **stats,
    }
    source_digest = digest({
        "protected_records": protected_records,
        "e6_sources": e6["sources"],
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
        "database_schema": database_schema,
        "captured_at": captured_at, "counts": counts, "source_digest": source_digest,
        "protected_records": protected_records,
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
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    return await build_plan(session, code_version=code_version,
                            identity=await database_identity(session))


def _verify_plan_integrity(plan: Mapping[str, Any]) -> None:
    unsigned = {key: value for key, value in plan.items()
                if key not in {"plan_digest", "batch_id"}}
    if digest(unsigned) != plan.get("plan_digest"):
        raise ValueError("计划摘要不一致（内容被修改）")
    if str(plan.get("rule_version")) != str(RULE_VERSION):
        raise ValueError("规则版本不兼容")
    if str(plan.get("batch_id")) != str(uuid.uuid5(uuid.NAMESPACE_URL, str(plan["plan_digest"]))):
        raise ValueError("批次 ID 与计划摘要不一致")
    if not isinstance(plan.get("protected_records"), dict):
        raise ValueError("计划缺少受保护业务记录")


async def apply_snapshot(
    session: AsyncSession, plan: Mapping[str, Any], *, code_version: str,
) -> dict[str, Any]:
    """事务内重建计划并核对，再按目标写入；已有批次只读返回。"""
    _verify_plan_integrity(plan)
    if str(plan.get("code_version")) != code_version:
        raise ValueError("代码版本与批准计划不一致，请重新 plan")
    if not plan.get("executable"):
        raise ValueError("计划不可执行：缺少可读的数据库集群身份")
    batch_id = uuid.UUID(str(plan["batch_id"]))
    async with session.begin():
        # Stop every writer (including direct CLI calls) between source comparison
        # and verification. Advisory lock alone does not fence ordinary writes.
        await session.execute(text("LOCK TABLE " + ", ".join(TABLES) + " IN SHARE ROW EXCLUSIVE MODE"))
        if await database_identity(session) != plan["database"]:
            raise ValueError("计划不属于当前数据库")
        revision = str((await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())
        schema = str((await session.execute(text("SELECT current_schema()"))).scalar_one())
        if revision != plan["schema_revision"] or schema != plan["database_schema"]:
            raise ValueError("数据库 schema / 迁移版本与批准计划不一致")
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                              {"key": LOCK_KEY})
        existing = await session.get(E8SnapshotBatch, batch_id)
        if existing is not None:
            await verify_snapshot(session, batch_id, commit=False)
            return {"batch_id": str(batch_id), "status": "already_applied",
                    **dict(existing.counts or {})}
        rebuilt = await build_plan(session, code_version=code_version,
                                   identity=plan["database"],
                                   captured_at=str(plan["captured_at"]))
        if rebuilt != dict(plan):
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
        result = await verify_snapshot(session, batch_id, commit=False, exact=True)
    return result


async def verify_snapshot(session: AsyncSession, batch_id: uuid.UUID, *,
                          commit: bool = True, exact: bool = False,
                          record_result: bool = True) -> dict[str, Any]:
    batch = await session.get(E8SnapshotBatch, batch_id)
    if batch is None:
        raise ValueError("E8 快照批次不存在")
    plan = dict(batch.plan or {})
    _verify_plan_integrity(plan)
    if str(batch.plan_digest) != str(plan["plan_digest"]):
        raise ValueError("批次计划摘要不一致")
    if (str(batch.id) != str(plan["batch_id"])
            or batch.rule_version != plan["rule_version"]
            or batch.code_version != plan["code_version"]
            or batch.schema_revision != plan["schema_revision"]
            or batch.database != plan["database"]
            or batch.source_digest != plan["source_digest"]
            or dict(batch.counts) != plan["counts"]):
        raise ValueError("批次字段与已批准计划不一致")
    actual_protected = await _protected_records(session)
    for table, expected_rows in plan["protected_records"].items():
        actual_by_key = {item["key"]: item["record"] for item in actual_protected[table]}
        if any(actual_by_key.get(item["key"]) != item["record"] for item in expected_rows):
            raise ValueError(f"{table} 受保护业务字段核验失败")
    # New runs / versions after cutover are expected. New hotspots / candidates
    # inside a protected historical run are not: they change its old result.
    protected_run_ids = {item["id"] for item in plan["targets"]["runs"]}
    protected_hotspot_ids = {item["id"] for item in plan["targets"]["run_hotspots"]}
    expected_hotspots = {item["key"] for item in plan["protected_records"]["run_hotspots"]}
    actual_hotspots = {item["key"] for item in actual_protected["run_hotspots"]
                       if json.loads(item["record"])["run_id"] in protected_run_ids}
    expected_matches = {item["key"] for item in plan["protected_records"]["run_matches"]}
    actual_matches = {item["key"] for item in actual_protected["run_matches"]
                      if json.loads(item["record"])["run_hotspot_id"] in protected_hotspot_ids}
    if actual_hotspots != expected_hotspots or actual_matches != expected_matches:
        raise ValueError("受保护历史运行的热点 / 候选集合核验失败")
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
            "SELECT material_snapshot::text AS s,evidence_meta::text AS e FROM run_matches "
            "WHERE run_hotspot_id=:rh AND material_id=:mid"),
            {"rh": target["run_hotspot_id"], "mid": target["material_id"]})).mappings().one_or_none()
        expected = target["material_snapshot"]
        actual_snapshot = json.loads(row["s"]) if row is not None and row["s"] is not None else None
        if (row is None or actual_snapshot != expected
                or json.loads(row["e"]) != target["evidence_meta"]):
            raise ValueError("run_matches 素材快照核验失败")
    for target in plan["targets"]["artifacts"]:
        row = (await session.execute(text(
            "SELECT evidence_snapshot::text AS e FROM chat_artifact_versions WHERE id=:id"),
            {"id": target["id"]})).scalar_one_or_none()
        if row is None or json.loads(row) != target["evidence_snapshot"]:
            raise ValueError("chat_artifact_versions 证据核验失败")
    if exact:
        for table, rows in actual_protected.items():
            if len(rows) != len(plan["protected_records"][table]):
                raise ValueError(f"E8 停写窗口计数核验失败：{table}")
    if record_result:
        batch.status = "verified"
        batch.verification = {"counts": dict(batch.counts), "verified": True,
                              "scope": "cutover_exact" if exact else "protected_records"}
        batch.verified_at = datetime.now(UTC)
        if commit:
            await session.commit()
        else:
            await session.flush()
    return {"batch_id": str(batch_id), "status": "verified", **dict(batch.counts)}

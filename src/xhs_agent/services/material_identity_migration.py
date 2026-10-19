"""E6 身份迁移：只读计划、事务执行及证据核验。

输入为隔离会话及显式根映射。计划确定性生成，不读取密钥、不调用模型、不移动文件。
apply 在写锁下重建计划并比对摘要，归档后合并；所有异常整体回滚。
归档采用 PostgreSQL 原生 JSON 表示，Decimal / vector 额外转成文本以保证可回填。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    Material,
    MaterialIdAlias,
    MaterialIdentityArchive,
    MaterialIdentityBatch,
    RunMatch,
)
from ..tools.material_paths import historical_key, validate_key
from .material_identity import lock_material_index

RULE_VERSION = 2
TABLES = (
    "materials", "run_matches", "run_hotspots", "runs", "hotspots", "chat_sessions",
    "chat_messages", "chat_context_events", "chat_artifact_versions", "chat_context_actions",
    "chat_image_parse_cache", "context_observations", "material_id_aliases",
)
PROTECTED = TABLES[2:-1]


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def original_key(table: str, record: dict[str, Any]) -> str:
    if table == "run_matches":
        return f"{record['run_hotspot_id']}:{record['material_id']}"
    return str(record.get("id") or record.get("old_id"))


def decode_record(table: str, record: dict[str, Any]) -> dict[str, Any]:
    """把归档里的 JSON 表示还原成可回填的 Python / ORM 值（字段级无损约定）。

    `snapshot` 对 PostgreSQL 无法直接 JSON 化的类型做了**显式文本编码**：
    `materials.embedding`（vector → "[0.1,0.2]"）、`materials.duration_s` / `run_matches.score`
    （numeric/Decimal → 十进制字符串）。这里提供严格逆变换；未知表原样返回，
    任何无法解析的值直接失败（不静默吞精度）。
    """
    from decimal import Decimal

    value = dict(record)
    if table == "materials":
        embedding = value.get("embedding")
        if isinstance(embedding, str) and embedding:
            try:
                value["embedding"] = [float(item) for item in json.loads(embedding)]
            except (ValueError, TypeError) as exc:
                raise ValueError("归档向量无法还原") from exc
        if isinstance(value.get("duration_s"), str):
            value["duration_s"] = Decimal(value["duration_s"])
    elif table == "run_matches" and isinstance(value.get("score"), str):
        value["score"] = Decimal(value["score"])
    return value


def _archive_rows(
    data: dict[str, list[dict[str, Any]]], plan: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """apply 要归档的完整行集合；plan 与 apply 共用同一份定义（含别名旧行）。"""
    affected = set(plan["affected_hotspots"])
    rows: list[tuple[str, dict[str, Any]]] = [
        ("materials", row) for row in data["materials"]
    ]
    rows += [("run_matches", row) for row in data["run_matches"]
             if row["run_hotspot_id"] in affected]
    rows += [("run_hotspots", row) for row in data["run_hotspots"]
             if row["id"] in affected]
    alias_changes = {item["old_id"] for item in plan.get("alias_updates", [])}
    rows += [("material_id_aliases", row) for row in data["material_id_aliases"]
             if row["old_id"] in alias_changes]
    return rows


def _manifest(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, str]]:
    return sorted(
        ({"entity_type": table, "original_key": original_key(table, record),
          "digest": digest(record)} for table, record in rows),
        key=encoded,
    )


async def database_identity(session: AsyncSession) -> dict[str, str]:
    """数据库身份（不含 DSN / 密码）。

    系统标识来自 `pg_control_system()`，需要 `pg_monitor` 及以上权限；部署账号权限不足时
    **不阻断**计划，但显式记录 `cluster_source=unavailable`，由审查者确认同一集群。
    """
    from sqlalchemy.exc import DBAPIError

    base = (await session.execute(text(
        "SELECT current_database() AS name, oid::text AS oid "
        "FROM pg_database WHERE datname = current_database()"))).mappings().one()
    cluster = ""
    source = "pg_control_system"
    try:
        async with session.begin_nested():
            cluster = str((await session.execute(text(
                "SELECT system_identifier::text FROM pg_control_system()"))).scalar_one())
    except DBAPIError:
        # savepoint 回滚后事务仍可用；权限不足是部署事实，不是数据错误
        source = "unavailable"
    return {"name": str(base["name"]), "oid": str(base["oid"]),
            "cluster": cluster, "cluster_source": source}


async def snapshot(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        expression = "to_jsonb(t)"
        if table == "materials":
            expression += (" || jsonb_build_object('embedding', t.embedding::text, "
                           "'duration_s', t.duration_s::text)")
        elif table == "run_matches":
            expression += " || jsonb_build_object('score', t.score::text)"
        # table 仅来自本模块固定白名单，不接受用户提供 SQL 标识符。
        rows = (await session.execute(text(f"SELECT {expression} FROM {table} t"))).scalars()
        result[table] = sorted([dict(row) for row in rows], key=encoded)
    return result


def _ordered_materials(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (
        row["source"] != "sidecar",
        -datetime.fromisoformat(row["indexed_at"]).timestamp(), row["id"]))


def build_plan(data: dict[str, list[dict[str, Any]]], *, identity: dict[str, str],
               mappings: list[dict[str, str]], relative_basis: str,
               revision: str, code_version: str) -> dict[str, Any]:
    """同一数据库快照与规则生成同一计划；缺失文件不影响保留 UUID 的选择。"""
    if relative_basis not in {"material", "mapped"}:
        raise ValueError("relative_basis 必须为 material 或 mapped")
    issues: list[dict[str, str]] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in data["materials"]:
        try:
            key = historical_key(row["path"], mappings, relative_basis=relative_basis)
            groups[key].append(row)
        except ValueError as exc:
            issues.append({"id": row["id"], "reason": str(exc)})
    folded: dict[str, str] = {}
    for key in sorted(groups):
        previous = folded.setdefault(key.casefold(), key)
        if previous != key:
            issues.append({"id": groups[key][0]["id"], "reason": "规范键存在大小写歧义"})
    mapping: dict[str, str] = {}
    material_groups: list[dict[str, Any]] = []
    expected_materials: list[dict[str, Any]] = []
    changed_ids: set[str] = set()
    for path, rows in sorted(groups.items()):
        ordered = _ordered_materials(rows)
        winner = ordered[0]
        mapping.update({row["id"]: winner["id"] for row in rows})
        fields = sorted({key for row in rows for key in row
                         if row.get(key) != winner.get(key)} - {"id", "path"})
        material_groups.append({"path": path, "canonical_id": winner["id"],
                                "members": sorted(row["id"] for row in rows),
                                "different_fields": fields})
        expected_materials.append({**winner, "path": path})
        if len(rows) > 1 or winner["path"] != path:
            changed_ids.update(row["id"] for row in rows)
    aliases = {old: new for old, new in mapping.items() if old != new}
    alias_updates: list[dict[str, str]] = []
    for row in data["material_id_aliases"]:
        if row["old_id"] in mapping:
            issues.append({"id": row["old_id"], "reason": "旧别名与活动素材 ID 冲突"})
        target = mapping.get(row["canonical_id"], row["canonical_id"])
        aliases[row["old_id"]] = target
        if str(row["canonical_id"]) != str(target):
            # 后续批次把旧别名重指向新的规范 ID：旧别名行也要进归档，保留迁移来源
            alias_updates.append({"old_id": row["old_id"],
                                  "from": row["canonical_id"], "to": target})
    if any(target in aliases or old == target for old, target in aliases.items()):
        issues.append({"id": "aliases", "reason": "别名含链或环"})
    affected = sorted({row["run_hotspot_id"] for row in data["run_matches"]
                       if row["material_id"] in changed_ids})
    candidate_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in data["run_matches"]:
        candidate_groups[(row["run_hotspot_id"], mapping.get(
            row["material_id"], row["material_id"]))].append(row)
    expected_matches = []
    decisions = []
    for (hotspot, target), rows in sorted(candidate_groups.items()):
        winner = sorted(rows, key=lambda row: (-Decimal(row["score"]), row["rank"],
                                               row["material_id"]))[0]
        expected_matches.append({**winner, "material_id": target})
        if hotspot in affected:
            decisions.append({"run_hotspot_id": hotspot, "canonical_id": target,
                              "original_material_id": winner["material_id"],
                              "rank": winner["rank"], "score": winner["score"],
                              "members": sorted(row["material_id"] for row in rows)})
    # 旧 JSON 引用盘点：受保护历史表里出现的素材 UUID（不批量改写，只作为预检证据）
    material_ids = sorted(str(row["id"]) for row in data["materials"])
    references: list[dict[str, Any]] = []
    for table in PROTECTED:
        for row in data[table]:
            body = encoded(row)
            hits = [material_id for material_id in material_ids if material_id in body]
            if hits:
                references.append({"table": table, "key": original_key(table, row),
                                   "material_ids": hits})
    keyframe_refs = [{"material_id": str(row["id"]),
                      "keyframes": [str(item) for item in (row.get("keyframes") or [])]}
                     for row in data["materials"] if row.get("keyframes")]
    affected_set = set(affected)
    affected_runs = sorted({
        str(row.get("run_id")) for row in data["run_hotspots"]
        if str(row.get("id")) in affected_set and row.get("run_id")
    })
    plan: dict[str, Any] = {
        "rule_version": RULE_VERSION, "database": identity, "schema_revision": revision,
        "code_version": code_version, "mappings": sorted(mappings, key=encoded),
        "relative_basis": relative_basis, "source_digest": digest(data),
        "source_tables": {table: {"count": len(rows), "digest": digest(rows)}
                          for table, rows in data.items()},
        "source_materials": [{"id": row["id"], "path": row["path"],
                              "digest": digest(row)} for row in data["materials"]],
        "groups": material_groups, "aliases": aliases, "affected_hotspots": affected,
        "alias_updates": sorted(alias_updates, key=encoded),
        "candidate_decisions": decisions, "issues": sorted(issues, key=encoded),
        # 预检证据：旧 JSON 引用 / 关键帧定位 / 关联运行（逐类盘点，不只整表摘要）
        "references": sorted(references, key=encoded),
        "keyframe_references": sorted(keyframe_refs, key=encoded),
        "affected_runs": affected_runs,
        "expected": {"materials": digest(sorted(expected_materials, key=encoded)),
                     "run_matches": digest(sorted(expected_matches, key=encoded))},
        # 逐行期望值：核验失败时可定位到具体行；也是 E8 交接的候选 / 素材快照
        "expected_records": {
            "materials": sorted(expected_materials, key=encoded),
            "run_matches": sorted(expected_matches, key=encoded),
        },
        "counts": {"materials_before": len(data["materials"]),
                   "materials_after": len(expected_materials),
                   "matches_before": len(data["run_matches"]),
                   "matches_after": len(expected_matches), "aliases_after": len(aliases),
                   "old_reference_rows": len(references),
                   "keyframe_materials": len(keyframe_refs),
                   "affected_runs": len(affected_runs)},
    }
    # 归档清单锚定到计划来源：实体集合、原主键与原始摘要都写进计划；
    # apply 归档后逐条比对，缺项 / 多项 / 改内容（即使重算自带摘要）都能被发现。
    plan["archive_manifest"] = _manifest(_archive_rows(data, plan))
    plan["plan_digest"] = digest(plan)
    plan["batch_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, plan["plan_digest"]))
    return plan


async def plan_migration(session: AsyncSession, mappings: list[dict[str, str]], *,
                         relative_basis: str = "material", code_version: str) -> dict[str, Any]:
    # 一次 repeatable-read 快照，避免 plan 内部读到两种版本；调用方必须使用新会话。
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    revision = str((await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())
    return build_plan(await snapshot(session), identity=await database_identity(session),
                      mappings=mappings, relative_basis=relative_basis,
                      revision=revision, code_version=code_version)


async def _verify_archive(session: AsyncSession, batch: MaterialIdentityBatch) -> int:
    rows = (await session.execute(select(MaterialIdentityArchive).where(
        MaterialIdentityArchive.batch_id == batch.id))).scalars().all()
    if len(rows) != batch.counts.get("archived"):
        raise ValueError("迁移归档条数不一致")
    expected = batch.plan.get("archive_manifest")
    if not isinstance(expected, list):
        raise ValueError("迁移计划缺少归档清单，无法证明原始完整性")
    actual = sorted(
        ({"entity_type": str(row.entity_type),
          "original_key": str(row.original_key), "digest": str(row.digest)}
         for row in rows),
        key=encoded,
    )
    if actual != expected:
        raise ValueError("迁移归档与计划清单不一致（缺项 / 多项 / 内容被改）")
    for row in rows:
        if row.provenance != "migration_capture" or digest(row.record) != row.digest:
            raise ValueError("迁移归档校验失败")
    return len(rows)


async def verify_migration(session: AsyncSession, batch_id: uuid.UUID, *,
                           exact: bool = True) -> dict[str, Any]:
    batch = await session.get(MaterialIdentityBatch, batch_id)
    if batch is None:
        raise ValueError("迁移批次不存在")
    archived = await _verify_archive(session, batch)
    if exact:
        later = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(MaterialIdentityBatch)
                    .where(MaterialIdentityBatch.created_at > batch.created_at)
                )
            ).scalar_one()
        )
        if later:
            raise ValueError(
                "该批次之后还有迁移批次：精确核验只适用于最新批次；"
                "历史批次请使用 --audit-only（归档与别名仍会核验）"
            )
    data = await snapshot(session)
    for row in data["materials"]:
        validate_key(row["path"])
    aliases = {row["old_id"]: row["canonical_id"] for row in data["material_id_aliases"]}
    if any(value in aliases for value in aliases.values()):
        raise ValueError("素材别名含链或环")
    # 批次核验只锚定本批次写入的映射：后续批次可以把目标再合并（A→B 之后 B→C，
    # 旧别名行会被重指向为 A→C），因此要求"本批 target 现在解析到 current"而不是全局相等。
    for old, target in batch.plan["aliases"].items():
        current = aliases.get(old)
        if current is None:
            raise ValueError("素材别名与迁移计划不一致")
        if str(current) != str(target):
            follow = aliases.get(str(target))
            if follow is None or str(follow) != str(current):
                raise ValueError("素材别名与迁移计划不一致")
    if exact:
        for table, expected in batch.plan["expected"].items():
            if digest(data[table]) != expected:
                raise ValueError(f"迁移后 {table} 与计划不一致；恢复业务后的变化需单独核对")
        for table in PROTECTED:
            if digest(data[table]) != batch.plan["source_tables"][table]["digest"]:
                raise ValueError(f"受保护历史表 {table} 被修改")
    return {"batch_id": str(batch.id), "status": "verified", "exact": exact,
            "archived": archived, **batch.counts}


async def apply_migration(session: AsyncSession, plan: dict[str, Any], *,
                          fault: Callable[[str], None] | None = None,
                          code_version: str | None = None) -> dict[str, Any]:
    """专用新会话执行；已有批次只读返回，不重新核写业务数据。"""
    unsigned = {key: value for key, value in plan.items() if key not in {"plan_digest", "batch_id"}}
    if digest(unsigned) != plan.get("plan_digest") or plan.get("rule_version") != RULE_VERSION:
        raise ValueError("计划被修改或规则版本不兼容")
    batch_id = uuid.UUID(str(plan["batch_id"]))
    if batch_id != uuid.uuid5(uuid.NAMESPACE_URL, plan["plan_digest"]):
        raise ValueError("批次 ID 与计划摘要不一致")
    if plan["issues"]:
        raise ValueError("存在未解决身份问题，禁止应用")
    async with session.begin():
        await lock_material_index(session)
        if await database_identity(session) != plan["database"]:
            raise ValueError("计划不属于当前数据库")
        existing = await session.get(MaterialIdentityBatch, batch_id)
        if existing is not None:
            await _verify_archive(session, existing)
            return {"batch_id": str(batch_id), "status": "already_applied", **existing.counts}
        # 停写仍是运维前提；表锁同时阻止遗留写入口绕过 advisory lock。
        await session.execute(text("LOCK TABLE " + ", ".join(TABLES) + " IN EXCLUSIVE MODE"))
        data = await snapshot(session)
        revision = str((await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())
        rebuilt = build_plan(data, identity=plan["database"], mappings=plan["mappings"],
                             relative_basis=plan["relative_basis"], revision=revision,
                             # 用**当前实现版本**重建，才能发现"计划来自另一份未提交实现"
                             code_version=code_version or plan["code_version"])
        if rebuilt != plan:
            raise ValueError("计划已过期或与源数据 / 实现版本不符，请重新 plan")
        affected = set(plan["affected_hotspots"])
        archives = _archive_rows(data, plan)
        counts = {**plan["counts"], "archived": len(archives)}
        session.add(MaterialIdentityBatch(id=batch_id, rule_version=RULE_VERSION,
                                         plan_digest=plan["plan_digest"], plan=plan,
                                         counts=counts, status="applied"))
        await session.flush()
        for table, record in archives:
            session.add(MaterialIdentityArchive(batch_id=batch_id, entity_type=table,
                        original_key=original_key(table, record), record=record, digest=digest(record)))
        await session.flush()
        # 先证明"归档完整且与计划清单一致"，再开始任何破坏性改写
        batch_row = await session.get(MaterialIdentityBatch, batch_id)
        assert batch_row is not None
        await _verify_archive(session, batch_row)
        if fault:
            fault("after_archive")
        originals = {original_key("run_matches", row): row for row in data["run_matches"]}
        if affected:
            await session.execute(delete(RunMatch).where(RunMatch.run_hotspot_id.in_(
                [uuid.UUID(value) for value in affected])))
        for choice in plan["candidate_decisions"]:
            record = dict(originals[f"{choice['run_hotspot_id']}:{choice['original_material_id']}"])
            record["material_id"] = choice["canonical_id"]
            await session.execute(text("INSERT INTO run_matches SELECT * FROM "
                "jsonb_populate_record(NULL::run_matches, CAST(:record AS jsonb))"),
                {"record": encoded(record)})
        if fault:
            fault("after_matches")
        duplicate_ids = [uuid.UUID(member) for group in plan["groups"] for member in group["members"]
                         if member != group["canonical_id"]]
        if fault:
            fault("before_delete")
        if duplicate_ids:
            await session.execute(delete(Material).where(Material.id.in_(duplicate_ids)))
        if fault:
            fault("after_delete")
        for group in plan["groups"]:
            await session.execute(update(Material).where(Material.id == uuid.UUID(group["canonical_id"]))
                                  .values(path=group["path"]))
        for old, target in sorted(plan["aliases"].items()):
            row = await session.get(MaterialIdAlias, uuid.UUID(old))
            if row is None:
                session.add(MaterialIdAlias(old_id=uuid.UUID(old), canonical_id=uuid.UUID(target),
                                            batch_id=batch_id))
            else:
                row.canonical_id = uuid.UUID(target)
        await session.flush()
        result = await verify_migration(session, batch_id)
    return result

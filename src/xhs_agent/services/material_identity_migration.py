"""E6 身份迁移：只读计划、事务执行及证据核验。

输入为隔离会话及显式根映射。计划确定性生成，不读取密钥、不调用模型、不移动文件。
apply 在写锁下重建计划并比对摘要，归档后合并；所有异常整体回滚。

规则 v3（R04）：
- 快照以 **PostgreSQL JSON 文本**为源，用 `Decimal` 解析所有非整数数值，禁止默认 float；
- 计划摘要 / 归档清单 / 期望记录使用同一个无损 JSON 编码（Decimal 输出为数字字面量）；
- 归档与候选重写直接用 JSON 文本绑定回 JSONB，不经过 Python float 或整行 ORM 重建；
- 旧规则（v2）批次保留按旧口径的核验能力，但结果明确标注旧规则的无损性限制。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
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

#: 当前规则版本；旧计划（低版本）必须重新生成
RULE_VERSION = 3
#: 旧规则（v2）批次仍可按旧口径核验，但输出会标注精度限制
LEGACY_RULE_VERSIONS = (2,)
TABLES = (
    "materials", "run_matches", "run_hotspots", "runs", "hotspots", "chat_sessions",
    "chat_messages", "chat_context_events", "chat_artifact_versions", "chat_context_actions",
    "chat_image_parse_cache", "context_observations", "material_id_aliases",
)
PROTECTED = TABLES[2:-1]


def encoded(value: Any) -> str:
    """确定性无损 JSON 编码：`Decimal` 输出为**数字字面量**，不经过 float。

    只接受 JSON 原生类型与 `Decimal`、`int`；出现 `float` 直接失败——默认 JSON 解码
    会把高精度 numeric 变成二进制浮点，是 R04 要杜绝的损失路径。
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON 编码拒绝非有限数值")
        # 固定点表示，避免 `1E+3` / `1E-7` 经 JSONB 归一化后文本不一致
        return format(value, "f")
    if isinstance(value, float):
        raise ValueError("JSON 编码拒绝 float（请用 Decimal 解析数值）")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(encoded(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(((str(key), item) for key, item in value.items()),
                       key=lambda pair: pair[0])
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + encoded(item)
            for key, item in items
        ) + "}"
    raise TypeError(f"无法无损编码的类型：{type(value).__name__}")


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def digest_rows(rows: Iterable[dict[str, Any]]) -> str:
    """一组记录的摘要：先按无损文本排序，再对连接后的文本取 SHA-256。"""
    texts = sorted(encoded(row) for row in rows)
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()


def legacy_encoded(value: Any) -> str:
    """旧规则（v2）的编码：默认 `json.dumps`（数值经 float 表示）。"""

    def _plain(item: Any) -> Any:
        if isinstance(item, Decimal):
            return float(item)
        if isinstance(item, dict):
            return {str(key): _plain(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [_plain(child) for child in item]
        return item

    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def legacy_digest(value: Any) -> str:
    return hashlib.sha256(legacy_encoded(value).encode("utf-8")).hexdigest()


def legacy_digest_rows(rows: Iterable[dict[str, Any]]) -> str:
    texts = sorted(legacy_encoded(row) for row in rows)
    return hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()


def original_key(table: str, record: dict[str, Any]) -> str:
    if table == "run_matches":
        return f"{record['run_hotspot_id']}:{record['material_id']}"
    return str(record.get("id") or record.get("old_id"))


def decode_record(table: str, record: dict[str, Any]) -> dict[str, Any]:
    """把归档里的 JSON 表示还原成可回填的 Python / ORM 值（字段级无损约定）。

    `snapshot` 只对 PostgreSQL 无法直接 JSON 化的类型做显式文本编码：
    `materials.embedding`（vector → "[0.1,0.2]"）。其余 numeric 以 JSON 数字保存，
    归档读取时不再经过默认 float；未知表原样返回，无法解析的值直接失败。
    """
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

    系统标识来自 `pg_control_system()`，需要 `pg_monitor` 及以上权限。**仅**把权限不足
    （`42501`）识别为 `cluster_source=unavailable`；其他数据库错误原样上抛，不能笼统当成
    权限问题。计划可在低权限下生成，但会标记 `executable=false`。
    """
    base = (await session.execute(text(
        "SELECT current_database() AS name, oid::text AS oid "
        "FROM pg_database WHERE datname = current_database()"))).mappings().one()
    cluster = ""
    source = "pg_control_system"
    try:
        async with session.begin_nested():
            cluster = str((await session.execute(text(
                "SELECT system_identifier::text FROM pg_control_system()"))).scalar_one())
    except DBAPIError as exc:
        original = getattr(exc, "orig", None)
        sqlstate = str(getattr(original, "sqlstate", "")
                       or getattr(original, "pgcode", "") or "")
        if sqlstate != "42501":
            raise
        source = "unavailable"
    return {"name": str(base["name"]), "oid": str(base["oid"]),
            "cluster": cluster, "cluster_source": source}


def identity_executable(identity: dict[str, str]) -> bool:
    """集群身份可读才允许 apply；低权限预检可以生成计划，但不可执行。"""
    return (str(identity.get("cluster_source") or "") == "pg_control_system"
            and bool(str(identity.get("cluster") or "")))


def require_cluster_identity(identity: dict[str, str], *, stage: str) -> None:
    if not identity_executable(identity):
        raise ValueError(
            f"{stage}无法读取数据库集群身份（需要 pg_monitor 或同等权限）："
            "禁止 apply 写入；请由具备权限的操作者配置最小权限后重新生成计划"
        )


async def snapshot(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """无损快照：JSON 文本 + `Decimal` 解析，禁止默认 float。

    唯一需要显式文本化的是 `materials.embedding`（pgvector 类型无法直接 JSON 化）；
    其余 numeric（含嵌套 JSON 中的数字）以 JSON 数字保存，Python 侧用 Decimal 保留精度。
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        expression = "to_jsonb(t)"
        if table == "materials":
            expression += " || jsonb_build_object('embedding', t.embedding::text)"
        # table 仅来自本模块固定白名单，不接受用户提供 SQL 标识符。
        rows = (await session.execute(
            text(f"SELECT ({expression})::text AS record FROM {table} t")
        )).scalars()
        parsed = [json.loads(raw, parse_float=Decimal) for raw in rows]
        result[table] = sorted(parsed, key=encoded)
    return result


async def legacy_snapshot(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """旧规则（v2）快照：默认 float 解码，duration_s / score 取文本。

    只用于核验规则 v2 的历史批次；新计划与新批次一律使用 `snapshot`。
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        expression = "to_jsonb(t)"
        if table == "materials":
            expression += (" || jsonb_build_object('embedding', t.embedding::text, "
                           "'duration_s', t.duration_s::text)")
        elif table == "run_matches":
            expression += " || jsonb_build_object('score', t.score::text)"
        rows = (await session.execute(
            text(f"SELECT ({expression})::text AS record FROM {table} t")
        )).scalars()
        parsed = [json.loads(raw) for raw in rows]
        result[table] = sorted(parsed, key=legacy_encoded)
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
                              "rank": winner["rank"], "score": str(winner["score"]),
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
        "source_tables": {table: {"count": len(rows), "digest": digest_rows(rows)}
                          for table, rows in data.items()},
        "source_materials": [{"id": row["id"], "path": row["path"],
                              "digest": digest(row)} for row in data["materials"]],
        "groups": material_groups, "aliases": aliases,
        "alias_records": sorted(
            ({"old_id": str(old), "canonical_id": str(target),
              "batch_id": next((str(row["batch_id"]) for row in data["material_id_aliases"]
                                if str(row["old_id"]) == str(old)), "this_batch")}
             for old, target in aliases.items()),
            key=encoded,
        ),
        "affected_hotspots": affected,
        "alias_updates": sorted(alias_updates, key=encoded),
        "candidate_decisions": decisions, "issues": sorted(issues, key=encoded),
        # 预检证据：旧 JSON 引用 / 关键帧定位 / 关联运行（逐类盘点，不只整表摘要）
        "references": sorted(references, key=encoded),
        "keyframe_references": sorted(keyframe_refs, key=encoded),
        "affected_runs": affected_runs,
        # 逐行期望值用**无损 JSON 文本**保存：计划本身保持 JSON 可序列化，
        # 核验时与实际行的无损文本逐条比较，不经过 float。
        "expected": {"materials": digest_rows(expected_materials),
                     "run_matches": digest_rows(expected_matches)},
        "expected_records": {
            "materials": sorted(encoded(row) for row in expected_materials),
            "run_matches": sorted(encoded(row) for row in expected_matches),
        },
        "counts": {"materials_before": len(data["materials"]),
                   "materials_after": len(expected_materials),
                   "matches_before": len(data["run_matches"]),
                   "matches_after": len(expected_matches), "aliases_after": len(aliases),
                   "old_reference_rows": len(references),
                   "keyframe_materials": len(keyframe_refs),
                   "affected_runs": len(affected_runs)},
        # R08：集群身份可读才可执行；低权限只读预检仍然允许生成计划
        "executable": identity_executable(identity),
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


def _count(mapping: dict[str, Any], key: str) -> int:
    """计数缺失时返回 -1；0 是合法计数，不能用 `or` 吞掉。"""
    if key not in mapping or mapping[key] is None:
        return -1
    return int(mapping[key])


async def _verify_archive(session: AsyncSession, batch: MaterialIdentityBatch) -> int:
    """核验归档：条数、计划清单、逐条摘要与 provenance。

    规则 v3 直接从 JSONB 文本按 Decimal 解析，避免 ORM 默认 float 解码；
    规则 v2 批次按旧口径（float / 文本）核验，并在结果中标注精度限制。
    """
    expected = batch.plan.get("archive_manifest")
    if not isinstance(expected, list):
        raise ValueError("迁移计划缺少归档清单，无法证明原始完整性")
    expected_keys = [(str(item.get("entity_type")), str(item.get("original_key")))
                     for item in expected]
    if len(set(expected_keys)) != len(expected_keys):
        raise ValueError("迁移归档清单包含重复的实体主键")
    if _count(batch.counts, "archived") != len(expected):
        raise ValueError("迁移归档条数与计划清单不一致")

    modern = int(batch.rule_version) >= 3
    if modern:
        rows = (await session.execute(text(
            "SELECT entity_type, original_key, record::text AS record, digest, provenance "
            "FROM material_identity_archive WHERE batch_id = :batch"),
            {"batch": batch.id})).mappings().all()
        if len(rows) != len(expected):
            raise ValueError("迁移归档条数不一致")
        actual: list[dict[str, str]] = []
        for row in rows:
            if str(row["provenance"]) != "migration_capture":
                raise ValueError("迁移归档 provenance 非法")
            record = json.loads(row["record"], parse_float=Decimal)
            if digest(record) != str(row["digest"]):
                raise ValueError("迁移归档校验失败（内容与摘要不一致）")
            actual.append({"entity_type": str(row["entity_type"]),
                           "original_key": str(row["original_key"]),
                           "digest": str(row["digest"])})
    else:
        rows_orm = (await session.execute(select(MaterialIdentityArchive).where(
            MaterialIdentityArchive.batch_id == batch.id))).scalars().all()
        if len(rows_orm) != len(expected):
            raise ValueError("迁移归档条数不一致")
        actual_legacy: list[dict[str, str]] = []
        for orm_row in rows_orm:
            if orm_row.provenance != "migration_capture":
                raise ValueError("迁移归档 provenance 非法")
            if legacy_digest(orm_row.record) != orm_row.digest:
                raise ValueError("迁移归档校验失败（旧规则）")
            actual_legacy.append({"entity_type": str(orm_row.entity_type),
                                  "original_key": str(orm_row.original_key),
                                  "digest": str(orm_row.digest)})
        actual = actual_legacy
    if sorted(actual, key=encoded) != sorted(expected, key=encoded):
        raise ValueError("迁移归档与计划清单不一致（缺项 / 多项 / 内容被改）")
    return len(actual)


def _verify_plan_integrity(batch: MaterialIdentityBatch) -> None:
    """核验批次内的计划摘要与计数，不信任批次里保存的计数。"""
    plan = batch.plan
    if not isinstance(plan, dict):
        raise ValueError("迁移批次缺少计划")
    unsigned = {key: value for key, value in plan.items()
                if key not in {"plan_digest", "batch_id"}}
    version = int(batch.rule_version)
    if version not in (*LEGACY_RULE_VERSIONS, RULE_VERSION):
        raise ValueError("不支持的迁移规则版本")
    plan_digest = legacy_digest(unsigned) if version in LEGACY_RULE_VERSIONS else digest(unsigned)
    if plan_digest != plan.get("plan_digest"):
        raise ValueError("迁移计划摘要不一致（计划内容被修改）")
    if str(plan.get("plan_digest")) != str(batch.plan_digest):
        raise ValueError("批次计划摘要与计划内容不一致")
    if int(plan.get("rule_version") or 0) != int(batch.rule_version):
        raise ValueError("批次规则版本与计划不一致")
    if str(batch.id) != str(plan.get("batch_id")) or batch.id != uuid.uuid5(
            uuid.NAMESPACE_URL, plan_digest):
        raise ValueError("批次 ID 与计划摘要不一致")
    if batch.counts != {**plan.get("counts", {}),
                        "archived": len(plan.get("archive_manifest") or [])}:
        raise ValueError("批次计数与计划不一致")
    manifest = plan.get("archive_manifest")
    if not isinstance(manifest, list) or _count(batch.counts, "archived") != len(manifest):
        raise ValueError("批次归档计数与计划清单不一致")
    aliases_after = _count(plan.get("counts") or {}, "aliases_after")
    if aliases_after != len(plan.get("aliases") or {}):
        raise ValueError("计划别名计数与别名集合不一致")
    if int(plan.get("rule_version") or 0) < 3:
        # 旧规则计划没有无损逐行期望；仍允许按其旧摘要核验，结果会标注限制
        return
    records = plan.get("expected_records")
    if not isinstance(records, dict):
        raise ValueError("迁移计划缺少逐行期望记录")
    materials_after = _count(plan.get("counts") or {}, "materials_after")
    matches_after = _count(plan.get("counts") or {}, "matches_after")
    if materials_after != len(records.get("materials") or []):
        raise ValueError("计划素材计数与逐行期望不一致")
    if matches_after != len(records.get("run_matches") or []):
        raise ValueError("计划候选计数与逐行期望不一致")


async def verify_migration(session: AsyncSession, batch_id: uuid.UUID, *,
                           exact: bool = True) -> dict[str, Any]:
    """核验一个迁移批次。

    - `exact=True`：只允许最新批次；比较**完整别名集合**、逐行期望记录与受保护历史表，
      并核验计划摘要 / 归档清单 / 计数；
    - `exact=False`（audit-only）：有限核验（归档、别名单向解析、链环、路径），
      不声明"当前数据与旧计划完全一致"。
    """
    batch = await session.get(MaterialIdentityBatch, batch_id)
    if batch is None:
        raise ValueError("迁移批次不存在")
    _verify_plan_integrity(batch)
    archived = await _verify_archive(session, batch)
    rule_version = int(batch.rule_version)
    limitations: list[str] = []
    if rule_version < 3:
        limitations.append(
            "该批次使用旧规则（v2）生成：归档与摘要按旧口径，不满足规则 v3 的无损保证；"
            "如需最新口径请重新 plan/apply 到新批次")
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
                "历史批次请使用 --audit-only（归档、别名与路径仍会核验）"
            )

    data = await snapshot(session) if rule_version >= 3 else await legacy_snapshot(session)
    for row in data["materials"]:
        validate_key(row["path"])
    material_ids = {str(row["id"]) for row in data["materials"]}
    aliases = {str(row["old_id"]): str(row["canonical_id"])
               for row in data["material_id_aliases"]}
    if any(old == canonical for old, canonical in aliases.items()):
        raise ValueError("素材别名含自环")
    if any(canonical in aliases for canonical in aliases.values()):
        raise ValueError("素材别名含链或环")
    conflicts = sorted(set(aliases) & material_ids)
    if conflicts:
        raise ValueError(f"素材别名与活动素材 ID 冲突：{conflicts[:5]}")

    planned = {str(old): str(target) for old, target in batch.plan["aliases"].items()}
    if exact:
        if aliases != planned:
            missing = sorted(set(planned) - set(aliases))
            extra = sorted(set(aliases) - set(planned))
            wrong = sorted(old for old in set(aliases) & set(planned)
                           if aliases[old] != planned[old])
            raise ValueError(
                "素材别名与迁移计划不一致："
                f"缺 {len(missing)}、多 {len(extra)}、目标不符 {len(wrong)}"
                + (f"（额外：{extra[:5]}）" if extra else "")
            )
        if rule_version >= 3:
            if any("batch_id" not in row for row in batch.plan.get("alias_records", [])):
                raise ValueError("旧 v3 批次缺少别名来源期望，无法精确核验；请用 audit-only 核对归档，"
                                 "未执行的旧计划必须重新生成")
            expected_records = [{**row, "batch_id": str(batch.id) if row["batch_id"] == "this_batch"
                                 else row["batch_id"]} for row in batch.plan["alias_records"]]
            if sorted(data["material_id_aliases"], key=encoded) != sorted(expected_records, key=encoded):
                raise ValueError("素材别名来源批次与迁移计划不一致")
    else:
        # 重放后续批次对旧别名的合法修改；当前 A→C、B→C 本身不是迁移证据。
        expected_aliases = dict(planned)
        later_batches = (await session.execute(select(MaterialIdentityBatch).where(
            MaterialIdentityBatch.created_at > batch.created_at).order_by(
                MaterialIdentityBatch.created_at, MaterialIdentityBatch.id))).scalars().all()
        for later_batch in later_batches:
            _verify_plan_integrity(later_batch)
            await _verify_archive(session, later_batch)
            archived_aliases = (await session.execute(select(MaterialIdentityArchive).where(
                MaterialIdentityArchive.batch_id == later_batch.id,
                MaterialIdentityArchive.entity_type == "material_id_aliases"))).scalars().all()
            previous = {str(row.record["old_id"]): str(row.record["canonical_id"])
                        for row in archived_aliases}
            next_aliases = later_batch.plan["aliases"]
            for old, target in list(expected_aliases.items()):
                next_target = next_aliases.get(old)
                if next_target != target:
                    if previous.get(old) != target or not next_target:
                        raise ValueError("历史别名重指向缺少后续批次归档证据")
                    expected_aliases[old] = str(next_target)
        if any(aliases.get(old) != target for old, target in expected_aliases.items()):
            raise ValueError("素材别名与有归档证据的迁移计划不一致")

    if exact:
        if rule_version >= 3:
            for table, expected_texts in batch.plan["expected_records"].items():
                actual_texts = sorted(encoded(row) for row in data[table])
                if actual_texts != list(expected_texts):
                    raise ValueError(
                        f"迁移后 {table} 与计划逐行期望不一致；恢复业务后的变化需单独核对")
        else:
            for table, expected_digest in batch.plan["expected"].items():
                if legacy_digest(data[table]) != expected_digest:
                    raise ValueError(
                        f"迁移后 {table} 与旧计划不一致（旧规则口径）")
        for table in PROTECTED:
            current = digest_rows(data[table]) if rule_version >= 3 \
                else legacy_digest(data[table])
            if current != batch.plan["source_tables"][table]["digest"]:
                raise ValueError(f"受保护历史表 {table} 被修改")
    return {"batch_id": str(batch.id), "status": "verified", "exact": exact,
            "rule_version": rule_version, "archived": archived,
            "limitations": limitations, **batch.counts}


async def apply_migration(session: AsyncSession, plan: dict[str, Any], *,
                          fault: Callable[[str], None] | None = None,
                          code_version: str | None = None) -> dict[str, Any]:
    """专用新会话执行；已有批次只读返回，不重新核写业务数据。

    R08：计划和目标库都必须有**可读取的集群身份**；低权限计划（`executable=false`）或
    目标库读不到身份时，在任何写入之前失败，不提供绕过参数。
    """
    unsigned = {key: value for key, value in plan.items() if key not in {"plan_digest", "batch_id"}}
    if digest(unsigned) != plan.get("plan_digest") or plan.get("rule_version") != RULE_VERSION:
        raise ValueError("计划被修改或规则版本不兼容；旧规则计划必须重新生成")
    batch_id = uuid.UUID(str(plan["batch_id"]))
    if batch_id != uuid.uuid5(uuid.NAMESPACE_URL, plan["plan_digest"]):
        raise ValueError("批次 ID 与计划摘要不一致")
    if plan["issues"]:
        raise ValueError("存在未解决身份问题，禁止应用")
    if not plan.get("executable"):
        raise ValueError(
            "计划生成时无法读取数据库集群身份（executable=false），禁止 apply；"
            "请由具备 pg_monitor 或同等权限的操作者重新生成计划")
    require_cluster_identity(plan["database"], stage="计划")

    async with session.begin():
        # 目标库身份必须在锁、归档或任何写入之前验证
        current_identity = await database_identity(session)
        require_cluster_identity(current_identity, stage="目标库")
        if current_identity != plan["database"]:
            raise ValueError("计划不属于当前数据库（名称 / OID / 集群标识不一致）")
        # 停写仍是运维前提；表锁同时阻止遗留写入口绕过 advisory lock。
        await lock_material_index(session)
        await session.execute(text("LOCK TABLE " + ", ".join(TABLES) + " IN EXCLUSIVE MODE"))
        # 拿到锁后再查一次：并发 apply 的后来者应读回已提交批次，而不是拿迁移后数据重建失败
        existing = await session.get(MaterialIdentityBatch, batch_id)
        if existing is not None:
            _verify_plan_integrity(existing)
            await _verify_archive(session, existing)
            return {"batch_id": str(batch_id), "status": "already_applied", **existing.counts}
        data = await snapshot(session)
        revision = str((await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())
        rebuilt = build_plan(data, identity=current_identity, mappings=plan["mappings"],
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
            await session.execute(text(
                "INSERT INTO material_identity_archive "
                "(batch_id, entity_type, original_key, record, digest, provenance) "
                "VALUES (:batch, :entity, :key, CAST(:record AS jsonb), :digest, "
                "'migration_capture')"),
                {"batch": batch_id, "entity": table,
                 "key": original_key(table, record), "record": encoded(record),
                 "digest": digest(record)})
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
            # 无损：直接绑定规范 JSON 文本，不经默认 float 或整行 ORM 重建
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

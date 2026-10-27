"""E8 第二阶段：解除 `run_matches.material_id` 对当前素材的删除级联。

候选行保留原 UUID 作为历史身份；当前素材是否存在由读侧动态查询，不写回历史字段。
回退会重新加回 CASCADE 外键，因此存在孤儿候选时拒绝 downgrade，避免历史被静默删除。
"""

import hashlib
import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

FK_NAME = "fk_run_matches_material_id_materials"

# Kept local to the revision: upgrading an old database must not import evolving
# asynchronous application services. JSONB equality preserves numeric precision.
BUSINESS_EXCLUDED = {
    "runs": ("execution_snapshot", "evidence_meta", "stage"),
    "run_hotspots": ("raw_text", "clue", "gap_advice", "retrieval_meta", "evidence_meta"),
    "run_matches": ("material_snapshot", "evidence_meta"),
    "chat_artifact_versions": ("evidence_snapshot", "updated_at"),
}
TARGETS = {
    "runs": ("runs", ("execution_snapshot", "evidence_meta")),
    "run_hotspots": ("run_hotspots", ("raw_text", "clue", "gap_advice", "retrieval_meta", "evidence_meta")),
    "run_matches": ("run_matches", ("material_snapshot", "evidence_meta")),
    "artifacts": ("chat_artifact_versions", ("evidence_snapshot",)),
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_verified_backfill(bind):
    """Fence nonempty upgrades until an exact v2 backfill was applied to this DB."""
    bind.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
    bind.execute(sa.text("LOCK TABLE runs,run_hotspots,run_matches,chat_artifact_versions,"
                         "e8_snapshot_batches IN SHARE ROW EXCLUSIVE MODE"))
    counts = {table: int(bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())
              for table in BUSINESS_EXCLUDED}
    if not any(counts.values()):
        return
    batch = bind.execute(sa.text(
        "SELECT id::text AS id,rule_version,code_version,database::text AS database,"
        "schema_revision,plan_digest,source_digest,plan::text AS plan,counts::text AS counts,"
        "status,verification::text AS verification FROM e8_snapshot_batches "
        "ORDER BY applied_at DESC,id DESC LIMIT 1")).mappings().one_or_none()
    if batch is None or batch["status"] != "verified" or batch["rule_version"] != 2:
        raise ValueError("缺少已核验的 E8 v2 回填批次")
    plan = json.loads(batch["plan"])
    unsigned = {key: value for key, value in plan.items()
                if key not in {"plan_digest", "batch_id"}}
    checksum = hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()
    if (checksum != plan.get("plan_digest") or checksum != batch["plan_digest"]
            or str(uuid.uuid5(uuid.NAMESPACE_URL, checksum)) != batch["id"]
            or batch["id"] != plan.get("batch_id")
            or plan.get("rule_version") != 2
            or plan.get("code_version") != batch["code_version"]
            or plan.get("schema_revision") != batch["schema_revision"]
            or plan.get("source_digest") != batch["source_digest"]
            or plan.get("counts") != json.loads(batch["counts"])
            or plan.get("database") != json.loads(batch["database"])
            or json.loads(batch["verification"]).get("verified") is not True):
        raise ValueError("E8 批次计划身份或摘要不一致")
    identity_row = bind.execute(sa.text(
        "SELECT current_database() AS name,oid::text AS oid FROM pg_database "
        "WHERE datname=current_database()")).mappings().one()
    identity = {"name": str(identity_row["name"]), "oid": str(identity_row["oid"]),
                "cluster": str(bind.execute(sa.text(
                    "SELECT system_identifier::text FROM pg_control_system()")).scalar_one()),
                "cluster_source": "pg_control_system"}
    if (plan.get("database") != identity or plan.get("schema_revision") != "0014"
            or plan.get("database_schema") != bind.execute(sa.text("SELECT current_schema()")).scalar_one()):
        raise ValueError("E8 回填计划不属于当前数据库身份或 0014 结构")
    for table, excluded in BUSINESS_EXCLUDED.items():
        expected = plan["protected_records"][table]
        if len(expected) != counts[table] or len({item["key"] for item in expected}) != counts[table]:
            raise ValueError(f"E8 回填未覆盖全部 {table} 历史记录")
        columns = ",".join(f"'{column}'" for column in excluded)
        for item in expected:
            record = json.loads(item["record"])
            key = (f"{record['run_hotspot_id']}:{record['material_id']}"
                   if table == "run_matches" else str(record["id"]))
            if key != item["key"]:
                raise ValueError("E8 受保护记录主键不一致")
            where = ("t.run_hotspot_id=CAST(:rh AS uuid) AND t.material_id=CAST(:mid AS uuid)"
                     if table == "run_matches" else "t.id=CAST(:id AS uuid)")
            params = {"rh": record.get("run_hotspot_id"), "mid": record.get("material_id"),
                      "id": record.get("id"), "record": item["record"]}
            if not bind.execute(sa.text(
                    f"SELECT (to_jsonb(t)-ARRAY[{columns}]::text[])=CAST(:record AS jsonb) "
                    f"FROM {table} t WHERE {where}"), params).scalar_one_or_none():
                raise ValueError(f"E8 {table} 受保护业务字段核验失败")
    for group, (table, fields) in TARGETS.items():
        expected = plan["targets"][group]
        if len(expected) != counts[table]:
            raise ValueError(f"E8 {table} 快照集合未覆盖全部历史")
        keys = set()
        columns = ",".join(f"'{column}',t.{column}" for column in fields)
        for item in expected:
            key = ((item["run_hotspot_id"], item["material_id"])
                   if table == "run_matches" else item["id"])
            if key in keys:
                raise ValueError("E8 快照集合主键重复")
            keys.add(key)
            where = ("t.run_hotspot_id=CAST(:rh AS uuid) AND t.material_id=CAST(:mid AS uuid)"
                     if table == "run_matches" else "t.id=CAST(:id AS uuid)")
            params = {"rh": item.get("run_hotspot_id"), "mid": item.get("material_id"),
                      "id": item.get("id"),
                      "record": _canonical({field: item[field] for field in fields})}
            if not bind.execute(sa.text(
                    f"SELECT jsonb_build_object({columns})=CAST(:record AS jsonb) "
                    f"FROM {table} t WHERE {where}"), params).scalar_one_or_none():
                raise ValueError(f"E8 {table} 回填快照核验失败")


def upgrade() -> None:
    try:
        _require_verified_backfill(op.get_bind())
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"E8 非空历史库尚未完成精确回填：停在 0014，执行 e8_snapshot.py plan/apply/verify；{exc}"
        ) from exc
    op.drop_constraint(FK_NAME, "run_matches", type_="foreignkey")


def downgrade() -> None:
    bind = op.get_bind()
    orphans = bind.execute(sa.text(
        "SELECT count(*) FROM run_matches AS m "
        "LEFT JOIN materials AS x ON x.id = m.material_id WHERE x.id IS NULL"
    )).scalar()
    if orphans:
        raise RuntimeError(
            f"存在 {orphans} 条历史候选引用已删除素材；回退会级联删除历史，已拒绝"
        )
    op.create_foreign_key(FK_NAME, "run_matches", "materials",
                          ["material_id"], ["id"], ondelete="CASCADE")

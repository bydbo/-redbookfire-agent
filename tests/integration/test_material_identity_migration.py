"""E6：真实 Postgres 的分阶段迁移、碰撞保全、故障回滚及旧 ID 生命周期。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from contextlib import suppress
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from xhs_agent.probe import check_material_identity
from xhs_agent.services.material_identity import current_material
from xhs_agent.services.material_identity_migration import (
    apply_migration,
    digest,
    encoded,
    plan_migration,
    snapshot,
    verify_migration,
)
from xhs_agent.services.materials import load_material, trash_material, update_material
from xhs_agent.services.runs import load_material_frames

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOTS = [{"flavor": "posix", "root": "/app/data/materials"},
         {"flavor": "windows", "root": "D:\\project\\data\\materials"}]
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"


@pytest_asyncio.fixture
async def identity_db(db_engine, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "0012")
    ids = {name: uuid.uuid4() for name in ("a", "b", "single", "h", "r", "rh", "chat")}
    async with db_engine.begin() as conn:
        for name, path, source in [
            ("a", "/app/data/materials/运动/a.mp4", "filename"),
            ("b", "D:\\project\\data\\materials\\运动\\a.mp4", "sidecar"),
            ("single", "/app/data/materials/missing.mp4", "filename"),
        ]:
            await conn.execute(text("INSERT INTO materials "
                "(id,path,source,title,duration_s,embedding,embedding_model) "
                "VALUES (:id,:path,:source,:title,12.34,CAST(:v AS vector),'fixed')"),
                {"id": ids[name], "path": path, "source": source, "title": name,
                 "v": "[" + ",".join(["0.1234567"] + ["0"] * 1023) + "]"})
        await conn.execute(text("INSERT INTO hotspots(id,raw_text,clue) VALUES (:id,'热点','{}')"),
                           {"id": ids["h"]})
        await conn.execute(text("INSERT INTO runs(id,job_id,status) VALUES (:id,'identity-job','succeeded')"),
                           {"id": ids["r"]})
        await conn.execute(text("INSERT INTO run_hotspots(id,run_id,hotspot_id,position,draft) "
            "VALUES(:id,:r,:h,1,jsonb_build_object('material_id',CAST(:m AS text),'body','旧稿'))"),
            {"id": ids["rh"], "r": ids["r"], "h": ids["h"], "m": str(ids["a"])})
        for material, rank, score in [("a", 4, "0.9000"), ("b", 1, "0.8000"), ("single", 3, "0.7000")]:
            await conn.execute(text("INSERT INTO run_matches(run_hotspot_id,material_id,rank,score,reasons) "
                "VALUES(:rh,:m,:rank,:score,'[\"原始理由\"]')"),
                {"rh": ids["rh"], "m": ids[material], "rank": rank, "score": Decimal(score)})
        await conn.execute(text("INSERT INTO chat_sessions(id,context_state) "
            "VALUES(:id,jsonb_build_object('object_refs',jsonb_build_array(jsonb_build_object("
            "'material_id',CAST(:m AS text)))))"), {"id": ids["chat"], "m": str(ids["a"])})
        await conn.execute(text("INSERT INTO chat_messages(session_id,role,content) "
            "VALUES(:id,'user','保持旧消息')"), {"id": ids["chat"]})
    return async_sessionmaker(db_engine, expire_on_commit=False), config, ids


async def make_plan(factory):
    async with factory() as session:
        return await plan_migration(session, ROOTS, code_version="fixture")


async def read_snapshot(factory):
    async with factory() as session:
        return await snapshot(session)


async def test_merge_archives_exact_values_and_preserves_history(identity_db):
    factory, config, ids = identity_db
    before = await read_snapshot(factory)
    plan = await make_plan(factory)
    assert plan == await make_plan(factory)
    async with factory() as session:
        result = await apply_migration(session, plan)
    assert result["materials_after"] == 2
    assert result["matches_after"] == 2
    await asyncio.to_thread(command.upgrade, config, "0013")
    async with factory() as session:
        canonical = await current_material(session, ids["a"])
        assert canonical.id == ids["b"]
        assert canonical.path == "运动/a.mp4"
        assert canonical.duration_s == Decimal("12.34")
        assert canonical.embedding_model == "fixed"
        assert await current_material(session, ids["single"]) is not None
        archive_rows = (await session.execute(text(
            "SELECT entity_type, record::text AS record, digest "
            "FROM material_identity_archive"))).mappings().all()
        assert len(archive_rows) == 7  # 3 素材 + 3 候选 + 1 热点
        for row in archive_rows:
            record = json.loads(str(row["record"]), parse_float=Decimal)
            assert digest(record) == str(row["digest"])
            # 数据能原样由 PostgreSQL JSON 再反序列化为对应行（含 vector / numeric / 时间）。
            if row["entity_type"] == "materials":
                restored = (await session.execute(text(
                    "SELECT (jsonb_populate_record(NULL::materials, CAST(:record AS jsonb))).duration_s"),
                    {"record": str(row["record"])})).scalar_one()
                assert restored == Decimal("12.34")
        check = await verify_migration(session, uuid.UUID(plan["batch_id"]))
        assert check["status"] == "verified"
    after = await read_snapshot(factory)
    for table in before.keys() - {"materials", "run_matches", "material_id_aliases"}:
        assert after[table] == before[table]
    assert {row["rank"] for row in after["run_matches"]} == {3, 4}
    async with factory() as session:
        replay = await apply_migration(session, plan)
    assert replay["status"] == "already_applied"
    with pytest.raises(RuntimeError, match="备份"):
        await asyncio.to_thread(command.downgrade, config, "0012")


async def test_archive_preserves_numeric_precision_and_isolated_refill(identity_db):
    """R04：嵌套 JSON / 高精度小数 / 大整数 / Unicode / 空值经归档与隔离回填后 DB 值一致。"""
    factory, _, ids = identity_db
    elements = ('{"weight": 0.123456789012345678901234567890, '
                '"big": 123456789012345678901234567890, '
                '"nested": {"ratio": 1.0000000000000000001, "null": null}, '
                '"unicode": "夜跑·球场"}')
    async with factory() as session:
        await session.execute(text(
            "UPDATE materials SET elements = CAST(:elements AS jsonb), "
            "size_bytes = 922337203685477580, title = '高精度 素材' WHERE id = :id"),
            {"elements": elements, "id": ids["single"]})
        await session.commit()
        before = (await session.execute(text(
            "SELECT elements::text AS elements, size_bytes, duration_s::text AS duration_s, "
            "mtime::text AS mtime, embedding::text AS embedding "
            "FROM materials WHERE id = :id"), {"id": ids["single"]})).mappings().one()

    plan = await make_plan(factory)
    assert plan["rule_version"] == 3
    encoded(plan)                       # 计划本身必须 JSON 可序列化且无 float

    async with factory() as session:
        await apply_migration(session, plan)
    async with factory() as session:
        archived = (await session.execute(text(
            "SELECT record::text AS record, digest FROM material_identity_archive "
            "WHERE entity_type = 'materials' AND original_key = :key"),
            {"key": str(ids["single"])})).mappings().one()
        record = json.loads(str(archived["record"]), parse_float=Decimal)
        assert digest(record) == str(archived["digest"])
        await session.execute(text(
            "CREATE TEMP TABLE restore_check (LIKE materials INCLUDING DEFAULTS) "
            "ON COMMIT DROP"))
        await session.execute(text(
            "INSERT INTO restore_check SELECT * FROM jsonb_populate_record("
            "NULL::restore_check, CAST(:record AS jsonb))"),
            {"record": str(archived["record"])})
        restored = (await session.execute(text(
            "SELECT elements::text AS elements, size_bytes, duration_s::text AS duration_s, "
            "mtime::text AS mtime, embedding::text AS embedding "
            "FROM restore_check"))).mappings().one()
        # 逐字段比较数据库真实值，而不是两份已损失精度的 Python 对象
        assert restored["elements"] == before["elements"]
        assert restored["size_bytes"] == before["size_bytes"] == 922337203685477580
        assert restored["duration_s"] == before["duration_s"]
        assert restored["mtime"] == before["mtime"]
        assert restored["embedding"] == before["embedding"]


@pytest.mark.parametrize("stage", ["after_archive", "after_matches", "before_delete", "after_delete"])
async def test_fault_rolls_back_every_table(identity_db, stage):
    factory, _, _ = identity_db
    before = await read_snapshot(factory)
    plan = await make_plan(factory)

    def fault(current):
        if current == stage:
            raise RuntimeError("injected")

    async with factory() as session:
        with pytest.raises(RuntimeError, match="injected"):
            await apply_migration(session, plan, fault=fault)
    assert await read_snapshot(factory) == before
    async with factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM material_identity_batches"))).scalar() == 0
    async with factory() as session:
        assert (await apply_migration(session, plan))["status"] == "verified"


async def test_stale_and_unknown_plans_are_non_mutating(identity_db):
    factory, config, _ = identity_db
    with pytest.raises(RuntimeError, match="旧格式"):
        await asyncio.to_thread(command.upgrade, config, "head")
    plan = await make_plan(factory)
    async with factory() as session:
        await session.execute(text("UPDATE materials SET title='changed'"))
        await session.commit()
    before = await read_snapshot(factory)
    async with factory() as session:
        with pytest.raises(ValueError, match="过期"):
            await apply_migration(session, plan)
    assert await read_snapshot(factory) == before
    async with factory() as session:
        unknown = await plan_migration(session, [], code_version="fixture")
    assert unknown["issues"]
    async with factory() as session:
        with pytest.raises(ValueError, match="未解决"):
            await apply_migration(session, unknown)
    assert await read_snapshot(factory) == before


async def test_extra_alias_and_tampered_plan_fail_exact_verify(identity_db):
    """R05：额外别名、计划摘要被改都必须让最新批次精确核验失败。"""
    factory, _, ids = identity_db
    plan = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)
    extra = uuid.uuid4()
    async with factory() as session:
        await session.execute(text(
            "INSERT INTO material_id_aliases (old_id, canonical_id, batch_id) "
            "VALUES (:old, :canon, :batch)"),
            {"old": extra, "canon": ids["b"], "batch": uuid.UUID(plan["batch_id"])})
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="别名"):
            await verify_migration(session, uuid.UUID(plan["batch_id"]))
    async with factory() as session:
        await session.execute(text("DELETE FROM material_id_aliases WHERE old_id = :old"),
                              {"old": extra})
        await session.execute(text(
            "UPDATE material_identity_batches SET plan = jsonb_set("
            "plan, '{counts,materials_after}', '999'::jsonb)"))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="摘要"):
            await verify_migration(session, uuid.UUID(plan["batch_id"]))


async def test_zero_alias_batch_verifies(identity_db):
    """0 个别名是合法计数，不能被 `or -1` 误判成缺失。"""
    factory, _, ids = identity_db
    async with factory() as session:
        await session.execute(text("DELETE FROM materials WHERE id=:id"), {"id": ids["b"]})
        await session.commit()
    plan = await make_plan(factory)
    assert plan["aliases"] == {} and plan["counts"]["aliases_after"] == 0
    async with factory() as session:
        result = await apply_migration(session, plan)
    assert result["aliases_after"] == 0
    async with factory() as session:
        assert (await verify_migration(session, uuid.UUID(plan["batch_id"])))["status"] == "verified"


async def test_concurrent_apply_creates_single_batch(identity_db):
    """CLI / worker / 同步共用同一互斥边界：并发 apply 只产生一个批次。"""
    factory, _, _ = identity_db
    plan = await make_plan(factory)

    async def run():
        async with factory() as session:
            return await apply_migration(session, plan)

    results = await asyncio.gather(run(), run())
    assert sorted(result["status"] for result in results) == ["already_applied", "verified"]
    async with factory() as session:
        assert (await session.execute(text(
            "SELECT count(*) FROM material_identity_batches"))).scalar() == 1


async def test_alias_survives_delete_without_rebinding_same_path(identity_db):
    factory, _, ids = identity_db
    plan = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)
    async with factory() as session:
        await session.execute(text("DELETE FROM materials WHERE id=:id"), {"id": ids["b"]})
        await session.execute(text("INSERT INTO materials(path) VALUES ('运动/a.mp4')"))
        await session.commit()
        assert await current_material(session, ids["a"]) is None
        assert (await session.execute(text("SELECT count(*) FROM material_id_aliases"))).scalar() == 1
        assert (await verify_migration(session, uuid.UUID(plan["batch_id"]), exact=False))["status"] == "verified"


async def test_archive_manifest_detects_tampering(identity_db):
    """归档必须锚定计划来源：缺项、多项、改内容（即使重算自带摘要）都能发现。"""
    factory, _, _ = identity_db
    plan = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)

    async with factory() as session:      # 改内容但同步重算自带摘要 → 清单摘要不匹配
        row = (await session.execute(text(
            "SELECT entity_type, original_key, record::text AS record "
            "FROM material_identity_archive LIMIT 1"))).mappings().one()
        record = json.loads(str(row["record"]), parse_float=Decimal)
        record["title"] = "tampered"
        await session.execute(text(
            "UPDATE material_identity_archive SET record = CAST(:record AS jsonb), "
            "digest = :digest WHERE entity_type = :entity AND original_key = :key"),
            {"record": encoded(record), "digest": digest(record),
             "entity": row["entity_type"], "key": row["original_key"]})
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="清单"):
            await verify_migration(session, uuid.UUID(plan["batch_id"]))

    async with factory() as session:      # 缺项
        await session.execute(text(
            "DELETE FROM material_identity_archive WHERE entity_type='materials' "
            "AND original_key = (SELECT min(original_key) FROM material_identity_archive "
            "WHERE entity_type='materials')"))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError):
            await verify_migration(session, uuid.UUID(plan["batch_id"]))


async def test_second_batch_repoints_alias_and_both_batches_verify(identity_db):
    """后续批次把规范素材再合并：旧别名直接重指向，旧批次核验仍可解释。"""
    factory, config, ids = identity_db
    first = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, first)
    # 新的人工说明素材与现有规范素材同键，且 sidecar 优先 → 再合并一次
    newer = uuid.uuid4()
    async with factory() as session:
        await session.execute(text(
            "INSERT INTO materials (id, path, source, title, indexed_at) "
            "VALUES (:id, 'D:\\project\\data\\materials\\运动/a.mp4', 'sidecar', '新人', "
            "now() + interval '1 hour')"), {"id": newer})
        await session.commit()
    second = await make_plan(factory)
    async with factory() as session:
        result = await apply_migration(session, second)
    assert result["materials_after"] == 2
    async with factory() as session:
        aliases = dict((await session.execute(text(
            "SELECT old_id::text, canonical_id::text FROM material_id_aliases"))).all())
        assert aliases[str(ids["a"])] == str(newer)
        assert aliases[str(ids["b"])] == str(newer)
        assert (await verify_migration(session, uuid.UUID(second["batch_id"])))["status"] == "verified"
        # 旧批次之后已有新批次：精确核验只适用于最新批次，历史批次用 audit-only
        with pytest.raises(ValueError, match="最新批次"):
            await verify_migration(session, uuid.UUID(first["batch_id"]))
        assert (await verify_migration(
            session, uuid.UUID(first["batch_id"]), exact=False))["status"] == "verified"
        assert (await current_material(session, ids["a"])).id == newer
        # 仅篡改来源批次，不改 old/target；最新精确核验也必须拒绝。
        await session.execute(text("UPDATE material_id_aliases SET batch_id=:batch WHERE old_id=:id"),
                              {"batch": uuid.UUID(second["batch_id"]), "id": ids["a"]})
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="来源批次"):
            await verify_migration(session, uuid.UUID(second["batch_id"]))
    await asyncio.to_thread(command.upgrade, config, "head")


async def test_audit_rejects_forged_repoint_without_later_batch(identity_db):
    factory, _, ids = identity_db
    plan = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)
    async with factory() as session:
        await session.execute(text("DELETE FROM materials WHERE id=:id"), {"id": ids["b"]})
        await session.execute(text("UPDATE material_id_aliases SET canonical_id=:id"),
                              {"id": ids["single"]})
        await session.execute(text("INSERT INTO material_id_aliases(old_id,canonical_id,batch_id) "
            "VALUES(:old,:target,:batch)"), {"old": ids["b"], "target": ids["single"],
                                             "batch": uuid.UUID(plan["batch_id"])})
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="归档证据"):
            await verify_migration(session, uuid.UUID(plan["batch_id"]), exact=False)


async def test_legacy_v2_batch_created_by_old_implementation_can_verify(identity_db, tmp_path):
    import importlib.util

    factory, _, _ = identity_db
    old = subprocess.run(["git", "show", "f12c110:src/xhs_agent/services/material_identity_migration.py"],
                         cwd=PROJECT_ROOT, capture_output=True, check=True)
    path = tmp_path / "legacy.py"
    path.write_bytes(old.stdout)
    spec = importlib.util.spec_from_file_location("xhs_agent.services._legacy_e6", path)
    assert spec and spec.loader
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    async with factory() as session:
        await session.execute(text("UPDATE run_matches SET hits=CAST(:value AS jsonb)"),
                              {"value": '[{"nested":{"fraction":0.123456789}}]'})
        await session.commit()
    async with factory() as session:
        plan = await legacy.plan_migration(session, ROOTS, code_version="legacy-fixture")
    async with factory() as session:
        await legacy.apply_migration(session, plan)
    async with factory() as session:
        result = await verify_migration(session, uuid.UUID(plan["batch_id"]))
        assert result["status"] == "verified" and result["rule_version"] == 2
        assert result["limitations"]


async def test_old_id_reaches_detail_edit_keyframes_and_trash(identity_db, tmp_path):
    """S6.11b：详情 / 编辑 / 关键帧 / 回收站共用别名解析；同路径新素材不接管旧引用。"""
    from xhs_agent.config import load_config

    factory, _, ids = identity_db
    materials_dir = tmp_path / "materials"
    materials_dir.mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[paths]\nmaterials_dir = "{materials_dir.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "runs" / "_index").as_posix()}"\n',
        encoding="utf-8")
    cfg = load_config(str(cfg_path))
    plan = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)
    async with factory() as session:
        await session.execute(text(
            "UPDATE materials SET keyframes = '[\"runs/_index/keyframes/x/kf.jpg\"]'::jsonb "
            "WHERE id = :id"), {"id": ids["b"]})
        await session.commit()
    async with factory() as session:
        detail = await load_material(session, cfg, ids["a"])
        assert detail is not None and detail["id"] == str(ids["b"])
        frames = await load_material_frames(session, ids["a"])
        assert frames == ["runs/_index/keyframes/x/kf.jpg"]
        edited = await update_material(session, cfg, ids["a"], title="别名编辑")
        assert edited is not None and edited["id"] == str(ids["b"])
        assert edited["title"] == "别名编辑"
        trashed = await trash_material(session, cfg, ids["a"])
        assert trashed is not None and trashed["file_missing"] is True
        assert await load_material(session, cfg, ids["a"]) is None
        # 同路径重新上传得到新 UUID，旧 ID 不按路径重绑定
        new_id = uuid.uuid4()
        await session.execute(text(
            "INSERT INTO materials (id, path) VALUES (:id, '运动/a.mp4')"),
            {"id": new_id})
        await session.commit()
        assert await load_material(session, cfg, ids["a"]) is None
        assert (await load_material(session, cfg, new_id)) is not None


async def test_startup_check_blocks_corrupt_identity(identity_db, db_engine):
    """S6.12：启动检查把非法身份作为阻断错误；正常迁移后的库放行。"""
    factory, _, ids = identity_db
    plan = await make_plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)
    assert await check_material_identity(db_engine) is None
    # 旧 ID 仍指向一个活动素材（别名与活动 ID 冲突）→ 阻断启动
    async with factory() as session:
        await session.execute(text(
            "INSERT INTO material_id_aliases (old_id, canonical_id, batch_id) "
            "VALUES (:old, :canon, :batch)"),
            {"old": ids["b"], "canon": ids["single"],
             "batch": uuid.UUID(plan["batch_id"])})
        await session.commit()
    problem = await check_material_identity(db_engine)
    assert problem is not None and problem.code == "E_MATERIAL_IDENTITY"


def _cli_env(postgres_dsn: str, cfg_path, tmp_path) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": postgres_dsn,
        "XHS_CONFIG_PATH": str(cfg_path),
        "PYTHONIOENCODING": "utf-8",
    }


async def test_cli_plan_apply_verify_on_temporary_database(
    identity_db, postgres_dsn, tmp_path
):
    """S6.10b/c：管理入口 plan → apply → verify 在临时库跑通；篡改计划被拒绝。"""
    factory, _, _ = identity_db
    materials_dir = tmp_path / "materials"
    materials_dir.mkdir()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[paths]\nmaterials_dir = "{materials_dir.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "runs" / "_index").as_posix()}"\n',
        encoding="utf-8")
    roots = tmp_path / "roots.json"
    roots.write_text(json.dumps({
        "relative_basis": "material",
        "roots": ROOTS,
    }), encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    env = _cli_env(postgres_dsn, cfg_path, tmp_path)
    project_root = Path(__file__).resolve().parents[2]
    script = str(project_root / "scripts" / "normalize_material_identity.py")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, script, *args], cwd=str(project_root),
                              env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    planned = run("plan", "--roots", str(roots), "--output", str(plan_path))
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["archive_manifest"] and "+worktree:" in plan["code_version"]
    assert plan_path.with_suffix(".md").exists()

    applied = run("apply", "--plan", str(plan_path))
    assert applied.returncode == 0, applied.stderr
    verified = run("verify", "--batch", plan["batch_id"])
    assert verified.returncode == 0, verified.stderr

    tampered = json.loads(plan_path.read_text(encoding="utf-8"))
    tampered["groups"][0]["path"] = "篡改/路径.mp4"
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    rejected = run("apply", "--plan", str(tampered_path))
    assert rejected.returncode == 1
    assert "未完成" in rejected.stdout or "未完成" in rejected.stderr


async def test_low_privilege_plan_is_readonly_and_not_executable(
    identity_db, db_engine, postgres_dsn
):
    """R08：低权限可生成只读计划（executable=false），但不能 apply。"""
    factory, _, _ = identity_db
    role = f"e6_limited_{uuid.uuid4().hex[:8]}"
    database = make_url(postgres_dsn).database

    async def _drop_role(conn) -> None:
        for statement in (
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role}",
            f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role}",
            f'REVOKE ALL PRIVILEGES ON DATABASE "{database}" FROM {role}',
            f"DROP ROLE IF EXISTS {role}",
        ):
            with suppress(Exception):
                await conn.execute(text(statement))

    async with db_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'limited'"))
        await conn.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO {role}'))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        await conn.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}"))
        # 模拟真实低权限部署：没有 pg_monitor，也不允许执行 pg_control_system()
        await conn.execute(text(
            "REVOKE EXECUTE ON FUNCTION pg_control_system() FROM PUBLIC"))
    limited_url = make_url(postgres_dsn).set(username=role, password="limited")
    engine = create_async_engine(limited_url.render_as_string(hide_password=False),
                                 poolclass=NullPool)
    try:
        limited_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with limited_factory() as session:
            plan = await plan_migration(session, ROOTS, code_version="fixture")
        assert plan["executable"] is False
        assert plan["database"]["cluster_source"] == "unavailable"
        async with limited_factory() as session:
            with pytest.raises(ValueError, match="集群身份|不可执行"):
                await apply_migration(session, plan)
    finally:
        await engine.dispose()
        async with db_engine.begin() as conn:
            await _drop_role(conn)
            await conn.execute(text(
                "GRANT EXECUTE ON FUNCTION pg_control_system() TO PUBLIC"))


async def test_identical_cluster_cannot_apply_foreign_plan(
    identity_db, db_engine, postgres_dsn, monkeypatch
):
    """R08：两个数据相同的独立集群不能交叉应用计划（集群标识不同，写入前失败）。"""
    factory, _, ids = identity_db
    plan = await make_plan(factory)
    container = PostgresContainer(
        POSTGRES_IMAGE, driver="asyncpg", username="xhs", password="xhs", dbname="xhs",
    ).with_volume_mapping(str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
    with container:
        second_dsn = container.get_connection_url()
        monkeypatch.setenv("DATABASE_URL", second_dsn)
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        await asyncio.to_thread(command.upgrade, config, "0012")
        engine = create_async_engine(second_dsn, poolclass=NullPool)
        try:
            second_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with second_factory() as session:
                for source_id, path, title in (
                    (ids["a"], "/app/data/materials/运动/a.mp4", "a"),
                    (ids["b"], "D:\\project\\data\\materials\\运动\\a.mp4", "b"),
                    (ids["single"], "/app/data/materials/missing.mp4", "single"),
                ):
                    await session.execute(text(
                        "INSERT INTO materials (id, path, source, title) "
                        "VALUES (:id, :path, 'filename', :title)"),
                        {"id": source_id, "path": path, "title": title})
                await session.commit()
            async with second_factory() as session:
                with pytest.raises(ValueError, match="不属于当前数据库"):
                    await apply_migration(session, plan)
            async with second_factory() as session:
                assert (await session.execute(text(
                    "SELECT count(*) FROM materials"))).scalar() == 3
                assert (await session.execute(text(
                    "SELECT count(*) FROM material_identity_batches"))).scalar() == 0
        finally:
            await engine.dispose()

"""E6：真实 Postgres 的分阶段迁移、碰撞保全、故障回滚及旧 ID 生命周期。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from xhs_agent.db.models import MaterialIdentityArchive
from xhs_agent.probe import check_material_identity
from xhs_agent.services.material_identity import current_material
from xhs_agent.services.material_identity_migration import (
    apply_migration,
    digest,
    plan_migration,
    snapshot,
    verify_migration,
)
from xhs_agent.services.materials import load_material, trash_material, update_material
from xhs_agent.services.runs import load_material_frames

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOTS = [{"flavor": "posix", "root": "/app/data/materials"},
         {"flavor": "windows", "root": "D:\\project\\data\\materials"}]


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
    await asyncio.to_thread(command.upgrade, config, "head")
    async with factory() as session:
        canonical = await current_material(session, ids["a"])
        assert canonical.id == ids["b"]
        assert canonical.path == "运动/a.mp4"
        assert canonical.duration_s == Decimal("12.34")
        assert canonical.embedding_model == "fixed"
        assert await current_material(session, ids["single"]) is not None
        archives = (await session.execute(select(MaterialIdentityArchive))).scalars().all()
        assert len(archives) == 7  # 3 素材 + 3 候选 + 1 热点
        for row in archives:
            assert digest(row.record) == row.digest
            # 数据能原样由 PostgreSQL JSON 再反序列化为对应行（含 vector / numeric / 时间）。
            if row.entity_type == "materials":
                restored = (await session.execute(text(
                    "SELECT (jsonb_populate_record(NULL::materials, CAST(:record AS jsonb))).duration_s"),
                    {"record": __import__("json").dumps(row.record)})).scalar_one()
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
        row = (await session.execute(select(MaterialIdentityArchive).limit(1))).scalar_one()
        record = dict(row.record)
        record["title"] = "tampered"
        row.record = record
        row.digest = digest(record)
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
    await asyncio.to_thread(command.upgrade, config, "head")


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

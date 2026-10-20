"""E8 快照回填：来源标签、确定性计划、幂等 apply 与整体回滚。"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from xhs_agent.services.e8_snapshot_migration import (
    apply_snapshot,
    build_plan,
    verify_snapshot,
)
from xhs_agent.services.material_identity_migration import database_identity, encoded
from xhs_agent.services.material_identity_migration import digest as e6_digest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def seed_snapshot_db(db_engine, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    await asyncio.to_thread(
        command.upgrade, Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    ids = {name: uuid.uuid4() for name in (
        "archived", "current", "missing", "run", "hotspot", "rh", "session", "artifact")}
    async with db_engine.begin() as conn:
        for key, rel, title, fp, model in (
            ("archived", "sport/archived.mp4", "old", "fp-old", "old-model"),
            ("current", "sport/current.mp4", "current", "fp-current", "new-model"),
        ):
            await conn.execute(text(
                "INSERT INTO materials (id,path,source,title,description,tags,duration_s,"
                "width,height,has_audio,size_bytes,fingerprint,indexed_at,keyframes,"
                "embedding_model) VALUES (:id,:path,'sidecar',:title,'desc',"
                "ARRAY['tag'],12.34,1080,1920,true,100,:fp,now(),'[]',:model)"),
                {"id": ids[key], "path": rel, "title": title, "fp": fp, "model": model})
        await conn.execute(text(
            "INSERT INTO runs (id,job_id,status,topk) VALUES (:id,:job,'succeeded',5)"),
            {"id": ids["run"], "job": f"e8-{uuid.uuid4()}"})
        await conn.execute(text(
            "INSERT INTO hotspots (id,raw_text,clue) VALUES (:id,'old hotspot','{\"why_it_works\": [\"x\"]}')"),
            {"id": ids["hotspot"]})
        await conn.execute(text(
            "INSERT INTO run_hotspots (id,run_id,hotspot_id,position,status,coverage,draft) "
            "VALUES (:id,:run,:hotspot,1,'succeeded','{\"ratio\": 0.5}',"
            "CAST(:draft AS jsonb))"),
            {"id": ids["rh"], "run": ids["run"], "hotspot": ids["hotspot"],
             "draft": json.dumps({"body": "old draft",
                                  "material_id": str(ids["archived"]),
                                  "titles": [{"text": "old", "style": "x"}]})})
        await conn.execute(text(
            "INSERT INTO run_matches (run_hotspot_id,material_id,rank,score,reasons) "
            "VALUES (:rh,:m,1,0.9,'[\"old reason\"]'), (:rh,:c,2,0.8,'[\"current\"]'), "
            "(:rh,:x,3,0.7,'[\"missing\"]')"),
            {"rh": ids["rh"], "m": ids["archived"], "c": ids["current"],
             "x": ids["missing"]})
        await conn.execute(text(
            "INSERT INTO chat_sessions (id,title) VALUES (:id,'E8')"),
            {"id": ids["session"]})
        await conn.execute(text(
            "INSERT INTO chat_artifact_versions (id,session_id,object_key,object_type,run_id,"
            "hotspot_position,request_key,status,version_no,content,source) VALUES "
            "(:id,:s,:key,'draft',:run,1,'init','succeeded',1,"
            "CAST(:content AS jsonb),'{}')"),
            {"id": ids["artifact"], "s": ids["session"], "run": str(ids["run"]),
             "key": f"draft:{ids['run']}:1", "content": json.dumps({"body": "old draft",
                                    "material_id": str(ids["archived"]),
                                    "titles": [{"text": "old", "style": "x"}]})})
        record = {
            "id": str(ids["archived"]), "path": "sport/archived.mp4", "type": "video",
            "title": "old", "description": "desc", "tags": ["tag"],
            "elements": [], "duration_s": Decimal("12.34"), "width": 1080, "height": 1920,
            "has_audio": True, "size_bytes": 100, "mtime": None, "source": "sidecar",
            "keyframes": [], "indexed_at": "2026-10-01T00:00:00+00:00",
            "fingerprint": "fp-old", "embedding_model": "old-model",
        }
        e6_batch = uuid.uuid4()
        await conn.execute(text(
            "INSERT INTO material_identity_batches (id,rule_version,plan_digest,plan,counts,"
            "status) VALUES (:id,3,:d,'{}','{}','applied')"),
            {"id": e6_batch, "d": uuid.uuid4().hex})
        await conn.execute(text(
            "INSERT INTO material_identity_archive (batch_id,entity_type,original_key,record,"
            "digest,provenance) VALUES (:b,'materials',:k,CAST(:r AS jsonb),:d,"
            "'migration_capture')"),
            {"b": e6_batch, "k": str(ids["archived"]), "r": encoded(record),
             "d": e6_digest(record)})
    return factory, ids


@pytest_asyncio.fixture
async def snapshot_db(db_engine, postgres_dsn, monkeypatch):
    return await seed_snapshot_db(db_engine, postgres_dsn, monkeypatch)


async def test_plan_labels_sources_and_apply_is_idempotent(snapshot_db):
    factory, ids = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
        assert plan["counts"]["materials_migration_capture"] == 1
        assert plan["counts"]["materials_cutover_capture"] == 1
        assert plan["counts"]["materials_missing"] == 1
        assert plan["counts"]["artifacts_complete"] == 1

    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")

    async with factory() as session:
        rows = (await session.execute(text(
            "SELECT material_id::text AS id, material_snapshot::text AS s,"
            " evidence_meta::text AS e FROM run_matches ORDER BY rank"))).mappings().all()
        assert rows[0]["s"] is not None
        assert json.loads(rows[0]["e"])["candidate"]["origin"] == "migration_capture"
        assert json.loads(rows[1]["e"])["candidate"]["origin"] == "cutover_capture"
        assert rows[2]["s"] is None
        assert json.loads(rows[2]["e"])["candidate"]["origin"] == "missing"
        artifact = (await session.execute(text(
            "SELECT evidence_snapshot::text FROM chat_artifact_versions"))).scalar_one()
        assert json.loads(artifact)["completeness"] == "complete"
    # 重放同一计划：不重复批次（新会话避免复用已开启事务）
    async with factory() as session:
        replay = await apply_snapshot(session, plan, code_version="fixture")
        assert replay["status"] == "already_applied"
        assert (await session.execute(text(
            "SELECT count(*) FROM e8_snapshot_batches"))).scalar() == 1


async def test_plan_tampering_and_stale_source_block_apply(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
        tampered = json.loads(json.dumps(plan))
        tampered["targets"]["runs"][0]["evidence_meta"]["execution"]["origin"] = "runtime"
        with pytest.raises(ValueError, match="摘要"):
            await apply_snapshot(session, tampered, code_version="fixture")
        assert (await session.execute(text(
            "SELECT count(*) FROM e8_snapshot_batches"))).scalar() == 0

    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await session.execute(text("UPDATE runs SET topk = 7"))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="过期|源数据|不属于"):
            await apply_snapshot(session, plan, code_version="fixture")
        assert (await session.execute(text(
            "SELECT count(*) FROM e8_snapshot_batches"))).scalar() == 0
        assert (await session.execute(text(
            "SELECT count(*) FROM run_hotspots WHERE raw_text IS NOT NULL"))).scalar() == 0


async def test_verify_detects_tampered_snapshot(snapshot_db):
    factory, ids = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))

    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text(
            "UPDATE run_hotspots SET evidence_meta = evidence_meta || '{\"clue\": "
            "{\"origin\": \"runtime\"}}'::jsonb"))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError):
            await verify_snapshot(session, uuid.UUID(plan["batch_id"]))

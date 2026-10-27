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
from test_material_identity_migration import identity_db as identity_db
from test_material_identity_migration import make_plan

from alembic import command
from xhs_agent.services.e8_snapshot_migration import (
    apply_snapshot,
    build_plan,
    plan_snapshot,
    verify_snapshot,
)
from xhs_agent.services.material_identity_migration import (
    apply_migration,
    database_identity,
    encoded,
)
from xhs_agent.services.material_identity_migration import digest as e6_digest
from xhs_agent.services.run_snapshots import canonical, missing_meta

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
            "INSERT INTO hotspots (id,raw_text,clue) VALUES (:id,'old hotspot',"
            "'{\"hotspot_raw\":\"old hotspot\",\"elements\":[{\"type\":\"topic\",\"value\":\"sport\"}],\"why_it_works\":[\"x\"],\"match_keywords\":[]}')"),
            {"id": ids["hotspot"]})
        await conn.execute(text(
            "INSERT INTO run_hotspots (id,run_id,hotspot_id,position,status,coverage,draft) "
            "VALUES (:id,:run,:hotspot,1,'succeeded','{\"ratio\": 0.5}',"
            "CAST(:draft AS jsonb))"),
            {"id": ids["rh"], "run": ids["run"], "hotspot": ids["hotspot"],
             "draft": json.dumps({"body": "old draft",
                                  "material_id": str(ids["archived"]),
                                  "titles": [{"text": "old", "style": "x"}],
                                  "tags": [], "cover_text": "", "first_3s": "",
                                  "shot_list": [], "compliance_notes": []})})
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
                                    "titles": [{"text": "old", "style": "x"}],
                                    "tags": [], "cover_text": "", "first_3s": "",
                                    "shot_list": [], "compliance_notes": []})})
        record = {
            "id": str(ids["archived"]), "path": "sport/archived.mp4", "type": "video",
            "title": "old", "description": "desc", "tags": ["tag"],
            "elements": [], "duration_s": Decimal("12.34"), "width": 1080, "height": 1920,
            "has_audio": True, "size_bytes": 100, "mtime": None, "source": "sidecar",
            "keyframes": [], "indexed_at": "2026-10-01T00:00:00+00:00",
            "fingerprint": "fp-old", "embedding_model": "old-model",
        }
        manifest = [{"entity_type": "materials", "original_key": str(ids["archived"]),
                     "digest": e6_digest(record)}]
        e6_plan = {"rule_version": 3, "counts": {"materials_after": 0, "matches_after": 0,
                   "aliases_after": 0}, "aliases": {}, "archive_manifest": manifest,
                   "candidate_decisions": [], "expected_records": {"materials": [], "run_matches": []}}
        e6_plan["plan_digest"] = e6_digest(e6_plan)
        e6_batch = uuid.uuid5(uuid.NAMESPACE_URL, e6_plan["plan_digest"])
        e6_plan["batch_id"] = str(e6_batch)
        await conn.execute(text(
            "INSERT INTO material_identity_batches (id,rule_version,plan_digest,plan,counts,"
            "status) VALUES (:id,3,:d,CAST(:p AS jsonb),CAST(:c AS jsonb),'applied')"),
            {"id": e6_batch, "d": e6_plan["plan_digest"], "p": encoded(e6_plan),
             "c": encoded({**e6_plan["counts"], "archived": 1})})
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
        integrity = plan["targets"]["run_hotspots"][0]["evidence_meta"]["candidate_integrity"]
        assert integrity["completeness"] == "partial"
        assert integrity["missing_fields"] == ["deleted_candidates_unknown"]

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


async def test_e6_merge_uses_original_candidate_archive(identity_db):
    factory, cfg, ids = identity_db
    e6_plan = await make_plan(factory)
    choice = next(item for item in e6_plan["candidate_decisions"]
                  if item["canonical_id"] == str(ids["b"]))
    assert choice["original_material_id"] == str(ids["a"])
    async with factory() as session:
        await apply_migration(session, e6_plan)
    await asyncio.to_thread(command.upgrade, cfg, "0014")
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    candidate = next(item for item in plan["targets"]["run_matches"]
                     if item["material_id"] == str(ids["b"]))
    assert candidate["material_snapshot"]["id"] == str(ids["a"])
    assert candidate["material_snapshot"]["original_material_id"] == str(ids["a"])
    assert candidate["material_snapshot"]["title"] == "a"
    assert candidate["evidence_meta"]["candidate"]["origin"] == "migration_capture"
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")

    await asyncio.to_thread(command.upgrade, cfg, "head")


async def test_code_and_schema_drift_block_apply(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="approved",
                                identity=await database_identity(session))
    async with factory() as session:
        with pytest.raises(ValueError, match="代码版本"):
            await apply_snapshot(session, plan, code_version="unapproved")
    async with factory() as session:
        await session.execute(text("UPDATE alembic_version SET version_num='0014'"))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="迁移版本"):
            await apply_snapshot(session, plan, code_version="approved")
        assert (await session.execute(text("SELECT count(*) FROM e8_snapshot_batches"))).scalar() == 0


@pytest.mark.parametrize("statement", [
    "UPDATE run_matches SET score=0.01,reasons='[\"changed\"]'",
    "UPDATE run_matches SET evidence_meta='{}'",
    "UPDATE run_hotspots SET draft='{\"body\":\"changed\"}'",
    "UPDATE run_hotspots SET coverage=jsonb_build_object('ratio',0.1)",
    "UPDATE runs SET cost_cny=1,prompt_tokens=99",
    "UPDATE chat_artifact_versions SET content='{\"body\":\"changed\"}'",
])
async def test_verify_protects_business_fields_and_evidence(snapshot_db, statement):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text(statement))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="核验"):
            await verify_snapshot(session, uuid.UUID(plan["batch_id"]))


async def test_audit_allows_new_business_rows_but_apply_exact_does_not(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text("INSERT INTO runs(job_id,status) VALUES('new-after-cutover','succeeded')"))
        await session.commit()
    async with factory() as session:
        result = await verify_snapshot(session, uuid.UUID(plan["batch_id"]))
        assert result["status"] == "verified"
    async with factory() as session:
        with pytest.raises(ValueError, match="停写窗口计数"):
            await verify_snapshot(session, uuid.UUID(plan["batch_id"]), exact=True)
    async with factory() as session:
        assert (await apply_snapshot(session, plan, code_version="fixture"))["status"] == "already_applied"


async def test_archive_manifest_tampering_cannot_be_self_signed(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        row = (await session.execute(text("SELECT record::text FROM material_identity_archive"))).scalar_one()
        record = json.loads(row, parse_float=Decimal)
        record["title"] = "self-signed corruption"
        await session.execute(text("UPDATE material_identity_archive SET record=CAST(:r AS jsonb),digest=:d"),
                              {"r": encoded(record), "d": e6_digest(record)})
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="计划清单"):
            await build_plan(session, code_version="fixture", identity=await database_identity(session))


async def test_verify_rejects_extra_candidate_in_protected_hotspot(snapshot_db):
    factory, ids = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text("INSERT INTO run_matches(run_hotspot_id,material_id,rank,score,reasons) "
                                   "VALUES(:rh,:m,4,0.1,'[\"extra\"]')"),
                              {"rh": ids["rh"], "m": uuid.uuid4()})
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="候选集合"):
            await verify_snapshot(session, uuid.UUID(plan["batch_id"]))


async def test_incomplete_legacy_draft_remains_missing(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        await session.execute(text("UPDATE chat_artifact_versions SET content='{\"body\":\"only body\"}'"))
        await session.commit()
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    evidence = plan["targets"]["artifacts"][0]["evidence_snapshot"]
    assert evidence["completeness"] == "missing"
    assert "base_draft" in evidence["missing_fields"]


async def test_batch_identity_tampering_is_rejected(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
        plan["batch_id"] = str(uuid.uuid4())
    async with factory() as session:
        with pytest.raises(ValueError, match="批次 ID"):
            await apply_snapshot(session, plan, code_version="fixture")


async def test_new_plan_preserves_already_captured_history(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text("UPDATE materials SET title='later current title'"))
        await session.execute(text("UPDATE hotspots SET clue=CAST(:clue AS jsonb)"),
                              {"clue": json.dumps({"later": True})})
        await session.commit()
    async with factory() as session:
        later = await build_plan(session, code_version="fixture",
                                 identity=await database_identity(session))
    assert later["targets"] == plan["targets"]


async def test_base_draft_original_identity_cannot_use_another_merged_material():
    from xhs_agent.services.e8_snapshot_migration import _artifact_evidence
    original_a, original_b = str(uuid.uuid4()), str(uuid.uuid4())
    material = {"id": original_a, "original_material_id": original_a, "path": "x.mp4",
                "type": "video", "title": "A", "description": "A", "tags": [],
                "elements": [], "source": "sidecar"}
    draft = {"material_id": original_b, "titles": [], "body": "B draft", "tags": [],
             "cover_text": "", "first_3s": "", "shot_list": [], "compliance_notes": []}
    clue = {"hotspot_raw": "hotspot", "elements": [{"type": "topic", "value": "sport"}],
            "why_it_works": [], "match_keywords": []}
    evidence = _artifact_evidence(captured_at="2026-10-27T00:00:00+00:00", run_id=str(uuid.uuid4()),
                                  position=1, content=draft, clue=clue, material=material,
                                  clue_origin="cutover_capture", material_origin="migration_capture",
                                  canonical_material_id=original_b)
    assert evidence["completeness"] == "missing"
    assert "material_identity" in evidence["missing_fields"]


@pytest.mark.parametrize("statement", [
    "UPDATE run_matches SET score=0.01,reasons='[\"corrupt\"]'",
    "UPDATE run_matches SET material_snapshot=jsonb_set(material_snapshot,'{title}','\"tampered\"')",
    "UPDATE run_hotspots SET raw_text='tampered raw'",
    "UPDATE run_hotspots SET coverage=jsonb_build_object('ratio',0.2)",
    "UPDATE chat_artifact_versions SET evidence_snapshot=jsonb_set(evidence_snapshot,'{base_draft,body}','\"tampered\"')",
])
async def test_new_plan_cannot_reapprove_corrupted_existing_evidence(snapshot_db, statement):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text(statement))
        await session.commit()
    async with factory() as session:
        with pytest.raises(ValueError, match="既有|核验"):
            await build_plan(session, code_version="fixture", identity=await database_identity(session))


async def test_frozen_missing_clue_does_not_borrow_later_shared_clue_for_artifact(snapshot_db):
    factory, ids = snapshot_db
    async with factory() as session:
        await session.execute(text("UPDATE run_hotspots SET clue=NULL,evidence_meta=CAST(:e AS jsonb)"),
                              {"e": canonical({"clue": missing_meta("run:missing", ["clue"])})})
        claim = {"token": "internal-execution-token", "generation": 0}
        await session.execute(text("UPDATE runs SET evidence_meta=CAST(:e AS jsonb)"),
                              {"e": canonical({"_execution_claim": claim})})
        await session.commit()
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    assert plan["targets"]["run_hotspots"][0]["clue"] is None
    evidence = plan["targets"]["artifacts"][0]["evidence_snapshot"]
    assert evidence["completeness"] == "missing" and "clue" in evidence["missing_fields"]
    assert plan["counts"]["artifacts_complete"] == 0
    assert plan["targets"]["runs"][0]["evidence_meta"]["_execution_claim"] == claim


async def test_new_plan_audits_prior_batches_without_writes_and_allows_new_runs(snapshot_db):
    factory, _ = snapshot_db
    async with factory() as session:
        plan = await build_plan(session, code_version="fixture",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="fixture")
    async with factory() as session:
        await session.execute(text("INSERT INTO runs(job_id,status) VALUES('new-run-after-cutover','succeeded')"))
        await session.commit()
        before = (await session.execute(text("SELECT to_jsonb(b)::text FROM e8_snapshot_batches b"))).scalar_one()
    async with factory() as session:
        later = await plan_snapshot(session, code_version="fixture")
        assert (await session.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"
        assert later["counts"]["runs"] == plan["counts"]["runs"] + 1
    async with factory() as session:
        after = (await session.execute(text("SELECT to_jsonb(b)::text FROM e8_snapshot_batches b"))).scalar_one()
        assert after == before

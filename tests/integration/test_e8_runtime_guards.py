"""E8 的阶段证据、写入竞争、配置绑定及一致性检索回归。"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_run_writeback import (
    CLUE_PAYLOAD,
    FAST_RETRY,
    FakeEmbedder,
    FakeProvider,
    run_for,
    seed_library,
    seed_material,
    start_run,
    write_config,
)

from xhs_agent.api.deps import get_config, get_session
from xhs_agent.api.main import create_app
from xhs_agent.db import Base
from xhs_agent.schemas import HotspotClue
from xhs_agent.services.retrieval import retrieve_candidates
from xhs_agent.services.runs import (
    RunRecorder,
    SnapshotWriteConflict,
    load_run,
    mark_run_failed,
)
from xhs_agent.tasks.analysis import run_job
from xhs_agent.tools.llm import LLMResult, StructuredCaller
from xhs_agent.workflows import analysis as workflow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine, reset_database):
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def test_draft_failure_preserves_explained_candidates(db_session, tmp_path):
    await seed_library(db_session)
    cfg = write_config(tmp_path)
    rid = await start_run(db_session, ["热点 A", "热点 B"])

    class DraftFailure(FakeProvider):
        async def complete(self, call):
            if call.task == "copy_draft":
                self.tasks.append(call.task)
                return LLMResult(text="", provider=self.name, model=self.model,
                                 error="draft unavailable")
            return await super().complete(call)

    await run_for(db_session, rid, cfg, provider=DraftFailure(), retry_policy=FAST_RETRY)
    detail = await load_run(db_session, uuid.UUID(rid), cfg)
    assert detail["execution_snapshot"]["llm"]["model"] == "fake-1"
    assert detail["execution_snapshot"]["configured_llm"]["model"] == cfg.llm.model
    for hs in detail["hotspots"]:
        assert hs["status"] == "failed" and hs["clue"]
        assert len(hs["candidates"]) == 3
        assert hs["gap_advice"] is not None
        assert hs["draft"] is None
    report = (Path(cfg.runs_dir()) / rid / "report.md").read_text(encoding="utf-8")
    assert report.index("热点 A") < report.index("热点 B")
    assert "模型理由" in report


async def test_generated_clue_is_saved_before_retrieval(db_session, tmp_path, monkeypatch):
    cfg = write_config(tmp_path)
    rid = await start_run(db_session, ["新热点"])

    async def unavailable(*args, **kwargs):
        raise ValueError("retrieval unavailable")

    monkeypatch.setattr(workflow, "retrieve_candidates", unavailable)
    await run_for(db_session, rid, cfg, provider=FakeProvider(), retry_policy=FAST_RETRY)
    detail = await load_run(db_session, uuid.UUID(rid), cfg)
    assert detail["hotspots"][0]["clue"]["why_it_works"]
    assert detail["hotspots"][0]["candidates"] == []
    assert "coverage" not in detail["hotspots"][0]["evidence"]


async def test_accepted_hotspot_cannot_be_replaced_before_run_end(db_session):
    rid = await start_run(db_session, ["first", "second"])
    recorder = RunRecorder(db_session, rid)
    await recorder.start({"llm": {"model": "A"}})
    args = {"clue": {"why_it_works": ["A"]}, "coverage": {}, "candidates": [],
            "draft": None, "status": "succeeded"}
    await recorder.hotspot_finished(1, **args)
    with pytest.raises(RuntimeError, match="终结"):
        await recorder.hotspot_finished(1, **{**args, "clue": {"why_it_works": ["B"]}})
    await db_session.rollback()
    await recorder.hotspot_finished(1, **args)
    detail = await load_run(db_session, uuid.UUID(rid))
    assert detail["hotspots"][0]["clue"] == args["clue"]


async def test_execution_drift_rejected_without_replacing_original(db_session):
    rid = await start_run(db_session, ["one"])
    recorder = RunRecorder(db_session, rid)
    await recorder.start({"llm": {"model": "A"}, "captured_at": "first"})
    with pytest.raises(RuntimeError, match="变化"):
        await recorder.start({"llm": {"model": "B"}, "captured_at": "later"})
    await db_session.rollback()
    await recorder.start({"llm": {"model": "A"}, "captured_at": "later"})
    assert (await recorder.execution())["llm"]["model"] == "A"


async def test_undeclared_generation_cannot_write_and_late_failure_keeps_success(db_session):
    rid = await start_run(db_session, ["one"])
    recorder = RunRecorder(db_session, rid)
    await recorder.start({}, generation=2)
    assert await mark_run_failed(db_session, rid, "old generation failure") is False
    assert (await load_run(db_session, uuid.UUID(rid)))["status"] == "running"
    with pytest.raises(RuntimeError, match="代次"):
        await recorder.checkpoint(1, "clue", {"clue": {"why_it_works": ["old"]}})
    await db_session.rollback()
    await recorder.hotspot_finished(1, clue={"why_it_works": ["current"]},
                                    coverage={}, candidates=[], draft=None,
                                    status="succeeded", generation=2)
    await recorder.finish(status="succeeded", totals={}, prompt_versions={}, errors=[], generation=2)
    assert await mark_run_failed(db_session, rid, "late failure") is False
    assert (await load_run(db_session, uuid.UUID(rid)))["status"] == "succeeded"


async def test_retrieval_reads_consistent_published_library(db_engine, db_session, tmp_path):
    await seed_library(db_session)
    await db_session.commit()
    cfg = write_config(tmp_path)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    class InsertBeforeRead(FakeEmbedder):
        async def embed(self, texts):
            async with factory() as other:
                await seed_material(other, path="new.mp4", title="new", description="new")
                await other.commit()
            return await super().embed(texts)

    outcome = await retrieve_candidates(db_session, cfg, HotspotClue.from_dict(CLUE_PAYLOAD),
                                        embedder=InsertBeforeRead())
    assert outcome.vector_coverage.total == outcome.index_summary["materials_count"] == 4
    assert not db_session.in_transaction()


async def test_concurrent_commit_during_recall_keeps_one_snapshot(db_engine, db_session,
                                                                tmp_path, monkeypatch):
    from xhs_agent.services import retrieval
    await seed_library(db_session)
    await db_session.commit()
    cfg = write_config(tmp_path)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    original = retrieval.literal_recall

    async def recall_then_insert(session: AsyncSession, *args, **kwargs):
        result = await original(session, *args, **kwargs)
        async with factory() as other:
            await seed_material(other, path="late.mp4", title="late", description="late")
            await other.commit()
        return result

    monkeypatch.setattr(retrieval, "literal_recall", recall_then_insert)
    outcome = await retrieve_candidates(db_session, cfg, HotspotClue.from_dict(CLUE_PAYLOAD),
                                        embedder=FakeEmbedder())
    assert outcome.vector_coverage.total == outcome.index_summary["materials_count"] == 3
    assert len(outcome.material_snapshots) == 3


async def test_duplicate_task_cannot_start_models_or_fail_active_owner(
        db_engine, db_session, postgres_dsn, tmp_path, monkeypatch):
    """两个真实任务会话竞争同代次，第二个任务不得执行模型，也不得终结合法 owner。"""
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    await seed_library(db_session)
    rid = await start_run(db_session, ["duplicate task"])
    cfg = write_config(tmp_path)
    job = (await load_run(db_session, uuid.UUID(rid), cfg))["run_id"]
    from sqlalchemy import select

    from xhs_agent.db.models import Run
    job_id = (await db_session.execute(select(Run.job_id).where(
        Run.id == uuid.UUID(job)))).scalar_one()
    await db_session.commit()
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def complete(self, call):
            if call.task == "hotspot_clue":
                entered.set()
                await release.wait()
            return await super().complete(call)

    first_provider, duplicate_provider = BlockingProvider(), FakeProvider()
    first = asyncio.create_task(run_job(
        job_id, cfg, caller=StructuredCaller(first_provider),
        embedder=FakeEmbedder(), sync_index=False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=15)
        with pytest.raises(SnapshotWriteConflict, match="认领"):
            await run_job(job_id, cfg, caller=StructuredCaller(duplicate_provider),
                          embedder=FakeEmbedder(), sync_index=False)
        assert duplicate_provider.tasks == []
        assert await mark_run_failed(db_session, rid, "duplicate exception",
                                     owner_token="different-owner") is False
    finally:
        release.set()
    assert await asyncio.wait_for(first, timeout=20) == "succeeded"
    db_session.expire_all()
    detail = await load_run(db_session, uuid.UUID(rid), cfg)
    assert detail["status"] == "succeeded"
    assert "_execution_claim" not in detail["evidence"]


@pytest.mark.parametrize("failed_task", ["hotspot_clue", "material_select", "copy_draft"])
async def test_failed_analysis_detail_is_real_http_200(db_engine, db_session, tmp_path,
                                                      failed_task):
    await seed_library(db_session)
    cfg = write_config(tmp_path)
    rid = await start_run(db_session, [f"failure at {failed_task}"])

    class FailAtTask(FakeProvider):
        async def complete(self, call):
            if call.task == failed_task:
                return LLMResult(text="", provider=self.name, model=self.model,
                                 error="injected upstream failure")
            return await super().complete(call)

    await run_for(db_session, rid, cfg, provider=FailAtTask(), retry_policy=FAST_RETRY)
    app = create_app(check_startup=False)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def fresh_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = fresh_session
    app.dependency_overrides[get_config] = lambda: cfg
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                           base_url="http://e8-test") as client:
        response = await client.get(f"/api/runs/{rid}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    hotspot = body["hotspots"][0]
    assert hotspot["status"] == "failed" and hotspot["error"]
    if failed_task == "copy_draft":
        assert hotspot["coverage"]["ratio"] >= 0
        assert len(hotspot["candidates"]) == 3 and hotspot["draft"] is None
    else:
        assert hotspot["coverage"] is None and hotspot["candidates"] == []
        assert (hotspot["clue"] is None) == (failed_task == "hotspot_clue")

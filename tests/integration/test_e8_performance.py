"""E8 性能记录：固定规模临时数据记录 plan/apply/verify、历史读取耗时、快照体积与查询数。"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from xhs_agent.services.e8_snapshot_migration import (
    apply_snapshot,
    build_plan,
    verify_snapshot,
)
from xhs_agent.services.material_identity_migration import database_identity
from xhs_agent.services.runs import load_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNS = 10
HOTSPOTS = 2
MATCHES = 5
MATERIALS = 30


@pytest_asyncio.fixture
async def perf_db(db_engine, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    await __import__("asyncio").to_thread(
        command.upgrade, Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with db_engine.begin() as conn:
        material_ids = []
        for index in range(MATERIALS):
            material_ids.append((await conn.execute(text(
                "INSERT INTO materials (path,title,description,tags,fingerprint,indexed_at) "
                "VALUES (:p,:t,'desc',ARRAY['tag'],:fp,now()) RETURNING id"),
                {"p": f"perf/{index}.mp4", "t": f"素材{index}",
                 "fp": f"fp-{index}"})).scalar_one())
        for index in range(RUNS):
            run_id = (await conn.execute(text(
                "INSERT INTO runs (job_id,status,topk) VALUES (:j,'succeeded',5) RETURNING id"),
                {"j": f"perf-{index}"})).scalar_one()
            for position in range(1, HOTSPOTS + 1):
                hotspot = (await conn.execute(text(
                    "INSERT INTO hotspots (raw_text,clue) VALUES (:r,'{}') RETURNING id"),
                    {"r": f"perf hotspot {index}-{position}"})).scalar_one()
                rh = (await conn.execute(text(
                    "INSERT INTO run_hotspots (run_id,hotspot_id,position,status,coverage,draft) "
                    "VALUES (:run,:h,:p,'succeeded','{\"ratio\": 1}',"
                    "'{\"body\": \"draft\", \"titles\": []}'::jsonb) RETURNING id"),
                    {"run": run_id, "h": hotspot, "p": position})).scalar_one()
                for rank in range(1, MATCHES + 1):
                    await conn.execute(text(
                        "INSERT INTO run_matches (run_hotspot_id,material_id,rank,score,reasons) "
                        "VALUES (:rh,:m,:rank,:score,'[\"reason\"]')"),
                        {"rh": rh, "m": material_ids[(index + rank) % MATERIALS],
                         "rank": rank, "score": 0.9 - rank * 0.05})
    return factory, db_engine


async def test_record_snapshot_and_read_cost(perf_db, tmp_path: Path) -> None:
    factory, engine = perf_db
    async with factory() as session:
        started = time.perf_counter()
        plan = await build_plan(session, code_version="perf",
                                identity=await database_identity(session))
        plan_s = time.perf_counter() - started
    async with factory() as session:
        started = time.perf_counter()
        await apply_snapshot(session, plan, code_version="perf")
        apply_s = time.perf_counter() - started
    async with factory() as session:
        started = time.perf_counter()
        await verify_snapshot(session, uuid.UUID(plan["batch_id"]))
        verify_s = time.perf_counter() - started

    async with factory() as session:
        run_id = (await session.execute(text(
            "SELECT id::text FROM runs ORDER BY created_at LIMIT 1"))).scalar_one()
    queries = {"n": 0}

    def count(*_args, **_kwargs):
        queries["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        async with factory() as session:
            started = time.perf_counter()
            detail = await load_run(session, uuid.UUID(run_id), None)
            read_s = time.perf_counter() - started
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    size = len(json.dumps(detail, ensure_ascii=False).encode("utf-8"))
    async with factory() as session:
        db_size = (await session.execute(text(
            "SELECT coalesce(sum(pg_column_size(material_snapshot)),0) + "
            "coalesce(sum(pg_column_size(evidence_meta)),0) FROM run_matches"))).scalar_one()

    payload = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "dataset": {"runs": RUNS, "hotspots": RUNS * HOTSPOTS,
                    "matches": RUNS * HOTSPOTS * MATCHES, "materials": MATERIALS},
        "seconds": {"plan": round(plan_s, 3), "apply": round(apply_s, 3),
                    "verify": round(verify_s, 3), "load_run": round(read_s, 4)},
        "snapshot_bytes": int(db_size),
        "run_detail_bytes": size,
        "load_run_queries": queries["n"],
        "counts": plan["counts"],
    }
    out = PROJECT_ROOT / "runs" / "_e8" / "performance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert payload["load_run_queries"] <= 8      # 批量候选 / 素材状态，避免逐候选查询

"""E6 性能记录（R04/R05 验收）：固定规模下记录 plan / apply / verify 与扫描耗时。

不设通过阈值：只留可复现的机器、数据规模、耗时与峰值内存，供 E8 交接比较。
"""

from __future__ import annotations

import asyncio
import json
import platform
import time
import tracemalloc
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from xhs_agent.config import load_config
from xhs_agent.services import materials as material_service
from xhs_agent.services.material_identity_migration import (
    apply_migration,
    plan_migration,
    verify_migration,
)
from xhs_agent.tools import materials as material_tools
from xhs_agent.tools.materials import scan_materials

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOTS = [{"flavor": "posix", "root": "/app/data/materials"},
         {"flavor": "windows", "root": "D:\\project\\data\\materials"}]
MATERIAL_PAIRS = 100
UNIQUE_MATERIALS = 100
MATCHES = 400


@pytest_asyncio.fixture
async def perf_db(db_engine, postgres_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    await asyncio.to_thread(command.upgrade, Config(str(PROJECT_ROOT / "alembic.ini")), "0012")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with db_engine.begin() as conn:
        for index in range(MATERIAL_PAIRS):
            relative = f"运动/片-{index:03d}.mp4"
            await conn.execute(text(
                "INSERT INTO materials (id, path, source, title) "
                "VALUES (:id, :path, 'filename', :title)"),
                {"id": uuid.uuid4(), "path": f"/app/data/materials/{relative}",
                 "title": f"片-{index}"})
            await conn.execute(text(
                "INSERT INTO materials (id, path, source, title) "
                "VALUES (:id, :path, 'sidecar', :title)"),
                {"id": uuid.uuid4(),
                 "path": "D:\\project\\data\\materials\\" + relative.replace("/", "\\"),
                 "title": f"片-{index}-win"})
        for index in range(UNIQUE_MATERIALS):
            await conn.execute(text(
                "INSERT INTO materials (id, path, source, title) "
                "VALUES (:id, :path, 'filename', :title)"),
                {"id": uuid.uuid4(), "path": f"美食/图-{index:03d}.jpg",
                 "title": f"图-{index}"})
        material_ids = (await conn.execute(text(
            "SELECT id FROM materials ORDER BY random() LIMIT 40"))).scalars().all()
        for index in range(100):
            hotspot = uuid.uuid4()
            run = uuid.uuid4()
            run_hotspot = uuid.uuid4()
            await conn.execute(text(
                "INSERT INTO hotspots (id, raw_text, clue) VALUES (:id, :text, '{}')"),
                {"id": hotspot, "text": f"性能热点-{index}"})
            await conn.execute(text(
                "INSERT INTO runs (id, job_id, status) VALUES (:id, :job, 'succeeded')"),
                {"id": run, "job": f"perf-{index:03d}"})
            await conn.execute(text(
                "INSERT INTO run_hotspots (id, run_id, hotspot_id, position) "
                "VALUES (:id, :run, :hotspot, 1)"),
                {"id": run_hotspot, "run": run, "hotspot": hotspot})
        for index in range(MATCHES):
            await conn.execute(text(
                "INSERT INTO run_matches (run_hotspot_id, material_id, rank, score, reasons) "
                "VALUES ((SELECT id FROM run_hotspots ORDER BY id OFFSET :offset LIMIT 1), "
                ":material, :rank, :score, '[\"性能\"]')"),
                {"offset": index % 100,
                 "material": material_ids[((index % 100) * 4 + index // 100) % len(material_ids)],
                 "rank": index // 100 + 1,
                 "score": Decimal(f"0.{7000 + index % 2000}")})
    return factory


async def test_record_plan_apply_verify_and_scan_cost(
    perf_db, tmp_path: Path, monkeypatch
) -> None:
    factory = perf_db
    tracemalloc.start()
    process_started = time.process_time()
    async with factory() as session:
        started = time.perf_counter()
        plan = await plan_migration(session, ROOTS, code_version="perf-fixture")
        plan_s = time.perf_counter() - started
    assert plan["issues"] == [] and plan["rule_version"] == 3

    async with factory() as session:
        started = time.perf_counter()
        result = await apply_migration(session, plan)
        apply_s = time.perf_counter() - started
    assert result["status"] == "verified"

    async with factory() as session:
        started = time.perf_counter()
        verified = await verify_migration(session, uuid.UUID(plan["batch_id"]))
        verify_s = time.perf_counter() - started
    assert verified["status"] == "verified"

    scan_root = tmp_path / "materials"
    scan_root.mkdir()
    for index in range(MATERIAL_PAIRS + UNIQUE_MATERIALS):
        (scan_root / f"扫描-{index:03d}.mp4").write_bytes(b"\x00" * 16)
    started = time.perf_counter()
    found = scan_materials(str(scan_root))
    scan_s = time.perf_counter() - started
    assert len(found) == MATERIAL_PAIRS + UNIQUE_MATERIALS
    # 实际索引与未变重索引；探测/抽帧固定，隔离文件不访问模型。
    config = tmp_path / "index.toml"
    config.write_text(f'[paths]\nmaterials_dir="{scan_root.as_posix()}"\n'
        f'index_dir="{(tmp_path / "index").as_posix()}"\n', encoding="utf-8")
    cfg = load_config(str(config))
    monkeypatch.setattr(material_service, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(material_tools.media, "probe", lambda path: {})
    monkeypatch.setattr(material_tools.media, "extract_keyframes", lambda *args, **kwargs: [])
    async with factory() as session:
        started = time.perf_counter()
        indexed = await material_service.sync_materials(session, cfg)
        index_s = time.perf_counter() - started
        started = time.perf_counter()
        unchanged = await material_service.sync_materials(session, cfg)
        reindex_s = time.perf_counter() - started
    assert indexed.added == 200 and unchanged.unchanged == 200
    assert unchanged.added == unchanged.updated == unchanged.deleted == 0

    payload = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "machine": platform.platform(),
        "python": platform.python_version(),
        "database": "PostgreSQL 16 + pgvector (testcontainers)",
        "dataset": {
            "materials": MATERIAL_PAIRS * 2 + UNIQUE_MATERIALS,
            "duplicate_pairs": MATERIAL_PAIRS,
            "run_matches": MATCHES,
            "hotspots": 100,
            "scan_files": MATERIAL_PAIRS + UNIQUE_MATERIALS,
        },
        "seconds": {"plan": round(plan_s, 3), "apply": round(apply_s, 3),
                    "verify": round(verify_s, 3), "scan": round(scan_s, 3),
                    "index": round(index_s, 3), "unchanged_reindex": round(reindex_s, 3),
                    "process_cpu": round(time.process_time() - process_started, 3)},
        "python_heap_peak_bytes": tracemalloc.get_traced_memory()[1],
        "counts": plan["counts"],
    }
    tracemalloc.stop()
    output = PROJECT_ROOT / "runs" / "_material_identity" / "performance.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))

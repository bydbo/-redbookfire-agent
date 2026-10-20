"""E8 恢复演练：备份含快照的库 → 另一隔离容器恢复 → 批次与快照可核验。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_e8_snapshot_migration import seed_snapshot_db
from testcontainers.community.postgres import PostgresContainer

from xhs_agent.services.e8_snapshot_migration import (
    apply_snapshot,
    build_plan,
    verify_snapshot,
)
from xhs_agent.services.material_identity_migration import database_identity

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"


async def test_e8_backup_restore_keeps_history(
    db_engine, postgres_dsn, monkeypatch, postgres_container: PostgresContainer,
    tmp_path: Path,
) -> None:
    factory, _ = await seed_snapshot_db(db_engine, postgres_dsn, monkeypatch)
    async with factory() as session:
        plan = await build_plan(session, code_version="restore",
                                identity=await database_identity(session))
    async with factory() as session:
        await apply_snapshot(session, plan, code_version="restore")

    dump = tmp_path / "e8.dump"
    dumped = postgres_container.exec([
        "pg_dump", "-U", "xhs", "-d", "xhs", "-Fc", "-f", "/tmp/e8.dump"])
    assert dumped.exit_code == 0, dumped.output
    postgres_container.copy_from_container("/tmp/e8.dump", dump)

    target = PostgresContainer(
        POSTGRES_IMAGE, driver="asyncpg", username="xhs", password="xhs", dbname="xhs",
    ).with_volume_mapping(str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
    with target:
        target.copy_into_container(dump, "/tmp/e8.dump")
        restored = target.exec([
            "pg_restore", "-U", "xhs", "-d", "xhs", "--clean", "--if-exists",
            "--no-owner", "--no-privileges", "/tmp/e8.dump"])
        assert restored.exit_code == 0, restored.output
        engine = create_async_engine(target.get_connection_url(), poolclass=NullPool)
        try:
            second = async_sessionmaker(engine, expire_on_commit=False)
            async with second() as session:
                assert (await session.execute(text(
                    "SELECT count(*) FROM e8_snapshot_batches"))).scalar() == 1
                assert (await session.execute(text(
                    "SELECT count(*) FROM run_matches "
                    "WHERE material_snapshot IS NOT NULL"))).scalar() == 2
                assert (await verify_snapshot(
                    session, uuid.UUID(plan["batch_id"])))["status"] == "verified"
                snapshot = (await session.execute(text(
                    "SELECT material_snapshot::text FROM run_matches "
                    "WHERE material_snapshot IS NOT NULL ORDER BY rank LIMIT 1"))).scalar_one()
                assert json.loads(snapshot)["title"] == "old"
        finally:
            await engine.dispose()

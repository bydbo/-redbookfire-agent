"""E6 恢复演练（R04/R05 验收）：备份 → 迁移 → 恢复到另一隔离库 → 旧代码读回。

备份 revision 是 0012（迁移前，绝对路径格式）；迁移在源库执行；恢复目标使用同镜像新容器。
旧代码通过 `git worktree` 检出上一提交，用 `PYTHONPATH` 指向其 `src/` 读取恢复库。
"""

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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from xhs_agent.services.material_identity_migration import apply_migration, snapshot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"
OLD_REVISION = "6e8dee5"        # 本批修复前的提交（含 E6 迁移实现）


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest_asyncio.fixture
async def identity_db(db_engine, postgres_dsn, monkeypatch):
    """与 test_material_identity_migration 相同的迁移前数据（0012 + 旧绝对路径）。"""
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    await asyncio.to_thread(command.upgrade, config, "0012")
    ids = {name: uuid.uuid4() for name in ("a", "b", "single", "h", "r", "rh", "chat", "version")}
    async with db_engine.begin() as conn:
        for name, path, source in [
            ("a", "/app/data/materials/运动/a.mp4", "filename"),
            ("b", "D:\\project\\data\\materials\\运动\\a.mp4", "sidecar"),
            ("single", "/app/data/materials/missing.mp4", "filename"),
        ]:
            await conn.execute(text(
                "INSERT INTO materials (id,path,source,title) "
                "VALUES (:id,:path,:source,:title)"),
                {"id": ids[name], "path": path, "source": source, "title": name})
        await conn.execute(text(
            "INSERT INTO hotspots(id,raw_text,clue) VALUES (:id,'热点','{}')"),
            {"id": ids["h"]})
        await conn.execute(text(
            "INSERT INTO runs(id,job_id,status) VALUES (:id,'identity-job','succeeded')"),
            {"id": ids["r"]})
        await conn.execute(text(
            "INSERT INTO run_hotspots(id,run_id,hotspot_id,position) "
            "VALUES(:id,:r,:h,1)"), {"id": ids["rh"], "r": ids["r"], "h": ids["h"]})
        for material, rank, score in (("a", 4, "0.9000"), ("b", 1, "0.8000"),
                                      ("single", 3, "0.7000")):
            await conn.execute(text(
                "INSERT INTO run_matches(run_hotspot_id,material_id,rank,score,reasons) "
                "VALUES(:rh,:m,:rank,:score,'[\"原始理由\"]')"),
                {"rh": ids["rh"], "m": ids[material], "rank": rank,
                 "score": Decimal(score)})
        await conn.execute(text("UPDATE run_hotspots SET draft=CAST(:draft AS jsonb)"),
                           {"draft": '{"title":"旧稿","body":"保留正文","tags":["历史"]}'})
        await conn.execute(text("INSERT INTO chat_sessions(id,context_state) "
            "VALUES(:id,CAST(:state AS jsonb))"), {"id": ids["chat"],
                "state": json.dumps({"facts": [{"value": "保留约束"}],
                                     "object_refs": [{"material_id": str(ids["a"])}]})})
        await conn.execute(text("INSERT INTO chat_messages(session_id,role,content) "
            "VALUES(:id,'user','保留旧消息')"), {"id": ids["chat"]})
        await conn.execute(text("INSERT INTO chat_artifact_versions "
            "(id,session_id,object_key,object_type,request_key,status,version_no,content) "
            "VALUES(:id,:session,'draft:fixture','draft','restore-fixture','succeeded',1,"
            "CAST(:content AS jsonb))"), {"id": ids["version"], "session": ids["chat"],
                "content": '{"title":"历史版本","body":"保留版本正文"}'})
    return async_sessionmaker(db_engine, expire_on_commit=False), config, ids


async def test_backup_migrate_restore_and_read_with_old_code(
    postgres_container: PostgresContainer, identity_db, tmp_path: Path
):
    factory, _, ids = identity_db
    dump_host = tmp_path / "pre-migration.dump"
    async with factory() as session:
        original_snapshot = await snapshot(session)

    # 1) 迁移前备份（0012，绝对路径 + 候选）
    dumped = postgres_container.exec([
        "pg_dump", "-U", "xhs", "-d", "xhs", "-Fc", "-f", "/tmp/e6-pre.dump"])
    assert dumped.exit_code == 0, dumped.output
    postgres_container.copy_from_container("/tmp/e6-pre.dump", dump_host)
    assert dump_host.stat().st_size > 0

    # 2) 源库执行迁移（合并 / 归档 / 别名）
    plan = await _plan(factory)
    async with factory() as session:
        await apply_migration(session, plan)

    # 3) 恢复到另一个隔离容器
    target = PostgresContainer(
        POSTGRES_IMAGE, driver="asyncpg", username="xhs", password="xhs", dbname="xhs",
    ).with_volume_mapping(str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
    with target:
        target.copy_into_container(dump_host, "/tmp/pre.dump")
        restored = target.exec([
            "pg_restore", "-U", "xhs", "-d", "xhs", "--clean", "--if-exists",
            "--no-owner", "--no-privileges", "/tmp/pre.dump"])
        assert restored.exit_code == 0, restored.output
        dsn = target.get_connection_url()

        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine)() as session:
                assert await snapshot(session) == original_snapshot
            async with engine.connect() as connection:
                version = (await connection.execute(text(
                    "SELECT version_num FROM alembic_version"))).scalar_one()
                assert version == "0012"
                paths = set((await connection.execute(text(
                    "SELECT path FROM materials"))).scalars())
                counts = (await connection.execute(text(
                    "SELECT (SELECT count(*) FROM materials), "
                    "(SELECT count(*) FROM run_matches), "
                    "(SELECT count(*) FROM hotspots)"))).one()
        finally:
            await engine.dispose()
        # 备份原样：绝对路径、候选与热点都在，迁移后的合并没有写回备份
        assert len(paths) == 3 and counts == (3, 3, 1)

        # 4) 用与备份 revision 匹配的旧代码读取恢复库
        worktree = tmp_path / "old-code"
        _git("worktree", "add", "--detach", str(worktree), OLD_REVISION)
        try:
            script = tmp_path / "read_old.py"
            script.write_text(
                "import asyncio, json, os\n"
                "from sqlalchemy import text\n"
                "from xhs_agent.db.session import create_engine_from_config\n"
                "from xhs_agent.config import load_config\n"
                "from sqlalchemy.ext.asyncio import async_sessionmaker\n"
                "from xhs_agent.services.runs import load_run, load_report_model\n"
                "from xhs_agent.services.chat import load_session\n"
                "from xhs_agent.services.artifact_versions import get_version\n"
                "import uuid\n"
                "async def main():\n"
                "    cfg = load_config()\n"
                "    engine = create_engine_from_config(cfg)\n"
                "    async with engine.connect() as conn:\n"
                "        rows = (await conn.execute(text(\n"
                "            'SELECT path FROM materials ORDER BY path'))).scalars().all()\n"
                "        matches = (await conn.execute(text(\n"
                "            'SELECT count(*) FROM run_matches'))).scalar_one()\n"
                "    async with async_sessionmaker(engine)() as session:\n"
                f"        run = await load_run(session, uuid.UUID('{ids['r']}'))\n"
                f"        report = await load_report_model(session, cfg, uuid.UUID('{ids['r']}'))\n"
                f"        chat = await load_session(session, uuid.UUID('{ids['chat']}'))\n"
                f"        version = await get_version(session, uuid.UUID('{ids['chat']}'), "
                f"uuid.UUID('{ids['version']}'))\n"
                "        assert len(run['hotspots'][0]['candidates']) == 3\n"
                "        assert run['hotspots'][0]['draft']['body'] == '保留正文'\n"
                "        assert report and chat['messages'][0]['content'] == '保留旧消息'\n"
                "        assert version.content['body'] == '保留版本正文'\n"
                "    await engine.dispose()\n"
                "    print(json.dumps({'paths': rows, 'matches': matches}))\n"
                "asyncio.run(main())\n", encoding="utf-8")
            config = tmp_path / "config.toml"
            config.write_text(
                f'[paths]\nmaterials_dir = "{(tmp_path / "materials").as_posix()}"\n'
                f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
                f'index_dir = "{(tmp_path / "runs" / "_index").as_posix()}"\n',
                encoding="utf-8")
            env = {
                **os.environ,
                "DATABASE_URL": dsn,
                "XHS_CONFIG_PATH": str(config),
                "PYTHONPATH": str(worktree / "src"),
                "PYTHONIOENCODING": "utf-8",
            }
            read = subprocess.run([sys.executable, str(script)], cwd=str(worktree), env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace")
            assert read.returncode == 0, read.stderr[-800:]
            payload = json.loads(read.stdout.strip().splitlines()[-1])
            assert set(payload["paths"]) == paths
            assert payload["matches"] == 3
        finally:
            _git("worktree", "remove", "--force", str(worktree))


async def _plan(factory):
    from xhs_agent.services.material_identity_migration import plan_migration

    roots = [{"flavor": "posix", "root": "/app/data/materials"},
             {"flavor": "windows", "root": "D:\\project\\data\\materials"}]
    async with factory() as session:
        return await plan_migration(session, roots, code_version="fixture")

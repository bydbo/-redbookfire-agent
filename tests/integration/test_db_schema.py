"""五张表与数据契约的逐项核对（S2.2）。

建表走 `Base.metadata.create_all`：正式建表在 S2.3 由 Alembic 迁移负责，
这里验证的是"模型 metadata 与 `docs/contracts/数据契约.md` 一致"。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM

# 本模块全部用例都是 async，模块级打 asyncio 标记（strict 模式下必须显式声明）
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# 表 → 列 → information_schema.columns.udt_name（数组类型带下划线前缀）
COLUMN_SPECS: dict[str, dict[str, str]] = {
    "materials": {
        "id": "uuid", "path": "text", "type": "text", "title": "text", "description": "text",
        "tags": "_text", "elements": "jsonb", "source": "text", "duration_s": "numeric",
        "width": "int4", "height": "int4", "has_audio": "bool", "size_bytes": "int8",
        "mtime": "timestamptz", "keyframes": "jsonb", "fingerprint": "text",
        "embedding": "vector", "embedding_model": "text", "indexed_at": "timestamptz",
    },
    "hotspots": {
        "id": "uuid", "raw_text": "text", "clue": "jsonb", "created_at": "timestamptz",
    },
    "runs": {
        "id": "uuid", "job_id": "text", "status": "text", "created_at": "timestamptz",
        "started_at": "timestamptz", "finished_at": "timestamptz", "llm_calls": "int4",
        "prompt_tokens": "int4", "completion_tokens": "int4", "cost_cny": "numeric",
        "latency_ms": "int4", "prompt_versions": "jsonb", "error": "text",
    },
    "run_hotspots": {
        "id": "uuid", "run_id": "uuid", "hotspot_id": "uuid", "position": "int4",
        "status": "text", "coverage": "jsonb", "draft": "jsonb", "error": "text",
    },
    "run_matches": {
        "run_hotspot_id": "uuid", "material_id": "uuid", "rank": "int4", "score": "numeric",
        "recall_sources": "_text", "hits": "jsonb", "missing": "jsonb", "reasons": "jsonb",
        "usage": "text",
    },
}

NULLABLE_COLUMNS: dict[str, set[str]] = {
    "materials": {"mtime", "fingerprint", "embedding", "embedding_model"},
    "hotspots": set(),
    "runs": {"started_at", "finished_at", "error"},
    "run_hotspots": {"draft", "error"},
    "run_matches": set(),
}

# 数据契约 §4.1 的 14 条索引（唯一约束在 PG 里也是索引）
EXPECTED_INDEXES = {
    "uq_materials_path", "ix_materials_fingerprint", "ix_materials_tags",
    "ix_materials_text_trgm", "ix_materials_embedding_hnsw", "ix_materials_indexed_at",
    "ix_hotspots_created_at", "uq_runs_job_id", "ix_runs_status", "ix_runs_created_at",
    "uq_run_hotspots_run_id_position", "ix_run_hotspots_hotspot_id",
    "uq_run_matches_run_hotspot_id_rank", "ix_run_matches_material_id",
}


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    """每个用例都重建一次表结构（`reset_database` 已在用例前清空过 schema）。"""
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed_minimal(conn) -> dict[str, object]:
    """插入一条最小可用链路，返回主键，供约束/级联用例复用。"""
    ids: dict[str, object] = {}
    ids["material"] = (await conn.execute(text(
        "INSERT INTO materials (path) VALUES ('D:/materials/seed.mp4') RETURNING id"))).scalar_one()
    ids["hotspot"] = (await conn.execute(text(
        "INSERT INTO hotspots (raw_text, clue) VALUES ('seed', '{}'::jsonb) RETURNING id"
    ))).scalar_one()
    ids["run"] = (await conn.execute(text(
        "INSERT INTO runs (job_id) VALUES ('job-seed') RETURNING id"))).scalar_one()
    ids["run_hotspot"] = (await conn.execute(text(
        "INSERT INTO run_hotspots (run_id, hotspot_id, position) VALUES (:run, :hotspot, 1) "
        "RETURNING id"), {"run": ids["run"], "hotspot": ids["hotspot"]})).scalar_one()
    return ids


class TestStructure:
    async def test_all_tables_exist(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            names = set((await conn.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars())
        assert set(COLUMN_SPECS) <= names

    async def test_columns_match_contract(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT table_name, column_name, udt_name, is_nullable "
                "FROM information_schema.columns WHERE table_schema = 'public'"))).all()
        actual: dict[str, dict[str, tuple[str, bool]]] = {}
        for table, column, udt, nullable in rows:
            if table in COLUMN_SPECS:
                actual.setdefault(table, {})[column] = (udt, nullable == "YES")
        for table, spec in COLUMN_SPECS.items():
            assert set(actual[table]) == set(spec), f"{table} 的列名与契约不一致"
            for column, udt in spec.items():
                assert actual[table][column][0] == udt, f"{table}.{column} 类型不一致"
                assert actual[table][column][1] == (column in NULLABLE_COLUMNS[table]), \
                    f"{table}.{column} 可空性与契约不一致"

    async def test_indexes_match_contract(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            defs = dict((await conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"))).all())
            options = dict((await conn.execute(text(
                "SELECT c.relname, c.reloptions FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public'"))).all())
        assert set(defs) >= EXPECTED_INDEXES, EXPECTED_INDEXES - set(defs)
        assert "USING gin (tags)" in defs["ix_materials_tags"]
        trgm = defs["ix_materials_text_trgm"]
        assert "USING gin" in trgm and "gin_trgm_ops" in trgm
        hnsw = defs["ix_materials_embedding_hnsw"]
        assert "USING hnsw" in hnsw and "vector_cosine_ops" in hnsw
        reloptions = " ".join(options["ix_materials_embedding_hnsw"] or [])
        assert "m=16" in reloptions
        assert "ef_construction=64" in reloptions

    async def test_defaults_are_applied_by_database(self, db_engine: AsyncEngine) -> None:
        async with db_engine.begin() as conn:
            row = (await conn.execute(text(
                "INSERT INTO materials (path) VALUES ('D:/materials/defaults.mp4') "
                "RETURNING type, source, title, description, tags, elements, width, height, "
                "has_audio, size_bytes, duration_s, indexed_at"))).one()
        assert row.type == "video"
        assert row.source == "filename"
        assert row.title == "" and row.description == ""
        assert list(row.tags) == []
        assert row.elements == []
        assert (row.width, row.height, row.size_bytes) == (0, 0, 0)
        assert row.has_audio is False
        assert float(row.duration_s) == 0.0
        assert row.indexed_at is not None

        async with db_engine.begin() as conn:
            run = (await conn.execute(text(
                "INSERT INTO runs (job_id) VALUES ('job-defaults') "
                "RETURNING status, llm_calls, cost_cny, created_at, prompt_versions"))).one()
        assert run.status == "queued"
        assert (run.llm_calls, float(run.cost_cny)) == (0, 0.0)
        assert run.created_at is not None
        assert run.prompt_versions == {}


class TestConstraints:
    @pytest.mark.parametrize("label, statement", [
        ("material type 枚举", "INSERT INTO materials (path, type) VALUES ('p1', 'text')"),
        ("material source 枚举", "INSERT INTO materials (path, source) VALUES ('p2', 'nope')"),
        ("material width 非负", "INSERT INTO materials (path, width) VALUES ('p3', -1)"),
        ("material height 非负", "INSERT INTO materials (path, height) VALUES ('p4', -1)"),
        ("material size_bytes 非负", "INSERT INTO materials (path, size_bytes) VALUES ('p5', -1)"),
        ("run status 枚举", "INSERT INTO runs (job_id, status) VALUES ('j1', 'xx')"),
        ("run cost_cny 非负", "INSERT INTO runs (job_id, cost_cny) VALUES ('j2', -1)"),
    ])
    async def test_check_constraints_reject(self, db_engine: AsyncEngine, label: str,
                                           statement: str) -> None:
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text(statement))

    async def test_run_table_constraints_reject(self, db_engine: AsyncEngine) -> None:
        async with db_engine.begin() as conn:
            ids = await seed_minimal(conn)
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO run_hotspots (run_id, hotspot_id, position) "
                    "VALUES (:r, :h, 0)"), {"r": ids["run"], "h": ids["hotspot"]})
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO run_hotspots (run_id, hotspot_id, position, status) "
                    "VALUES (:r, :h, 2, 'nope')"), {"r": ids["run"], "h": ids["hotspot"]})

        match = ("INSERT INTO run_matches (run_hotspot_id, material_id, rank, score, reasons) "
                 "VALUES (:rh, :m, :rank, :score, :reasons)")
        cases = [
            {"rh": ids["run_hotspot"], "m": ids["material"], "rank": 0, "score": 0.5,
             "reasons": '[{"why": "x"}]'},                       # rank >= 1
            {"rh": ids["run_hotspot"], "m": ids["material"], "rank": 1, "score": 1.5,
             "reasons": '[{"why": "x"}]'},                       # score ∈ [0,1]
            {"rh": ids["run_hotspot"], "m": ids["material"], "rank": 2, "score": 0.5,
             "reasons": "[]"},                                   # reasons 非空（可解释性硬约束）
        ]
        for params in cases:
            with pytest.raises(IntegrityError):
                async with db_engine.begin() as conn:
                    await conn.execute(text(match), params)

    async def test_unique_constraints_reject_duplicates(self, db_engine: AsyncEngine) -> None:
        async with db_engine.begin() as conn:
            ids = await seed_minimal(conn)
            await conn.execute(text(
                "INSERT INTO run_matches (run_hotspot_id, material_id, rank, score, reasons) "
                "VALUES (:rh, :m, 1, 0.5, '[{\"why\": \"x\"}]')"),
                {"rh": ids["run_hotspot"], "m": ids["material"]})
        duplicates = [
            ("INSERT INTO materials (path) VALUES ('D:/materials/seed.mp4')", None),
            ("INSERT INTO runs (job_id) VALUES ('job-seed')", None),
            ("INSERT INTO run_hotspots (run_id, hotspot_id, position) VALUES (:r, :h, 1)",
             {"r": ids["run"], "h": ids["hotspot"]}),
            ("INSERT INTO run_matches (run_hotspot_id, material_id, rank, score, reasons) "
             "VALUES (:rh, :m, 1, 0.5, '[{\"why\": \"x\"}]')",
             {"rh": ids["run_hotspot"], "m": ids["material"]}),
        ]
        for statement, params in duplicates:
            with pytest.raises(IntegrityError):
                async with db_engine.begin() as conn:
                    await conn.execute(text(statement), params or {})

    async def test_cascade_and_restrict(self, db_engine: AsyncEngine) -> None:
        async with db_engine.begin() as conn:
            ids = await seed_minimal(conn)
            await conn.execute(text(
                "INSERT INTO run_matches (run_hotspot_id, material_id, rank, score, reasons) "
                "VALUES (:rh, :m, 1, 0.5, '[{\"why\": \"x\"}]')"),
                {"rh": ids["run_hotspot"], "m": ids["material"]})

        # 1) RESTRICT：热点仍被 run_hotspots 引用，删不掉
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text("DELETE FROM hotspots WHERE id = :h"),
                                   {"h": ids["hotspot"]})

        # 2) CASCADE：删 run → run_hotspots 与 run_matches 一起消失
        async with db_engine.begin() as conn:
            await conn.execute(text("DELETE FROM runs WHERE id = :r"), {"r": ids["run"]})
            remaining = (await conn.execute(text(
                "SELECT (SELECT count(*) FROM run_hotspots) AS hotspots, "
                "(SELECT count(*) FROM run_matches) AS matches"))).one()
        assert (remaining.hotspots, remaining.matches) == (0, 0)


class TestVector:
    async def test_accepts_contract_dimension(self, db_engine: AsyncEngine) -> None:
        vector = "[" + ",".join(["0.0"] * EMBEDDING_DIM) + "]"
        async with db_engine.begin() as conn:
            row = (await conn.execute(text(
                "INSERT INTO materials (path, embedding, embedding_model) "
                "VALUES ('D:/materials/vector.mp4', CAST(:v AS vector), 'text-embedding-v3') "
                "RETURNING embedding"), {"v": vector})).one()
        assert row.embedding is not None

    async def test_rejects_wrong_dimension(self, db_engine: AsyncEngine) -> None:
        vector = "[" + ",".join(["0.0"] * (EMBEDDING_DIM + 1)) + "]"
        # pgvector 的维度校验在服务端抛 DataError，SQLAlchemy 归类为 DBAPIError
        with pytest.raises(DBAPIError):
            async with db_engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO materials (path, embedding) VALUES ('p-vec', CAST(:v AS vector))"),
                    {"v": vector})


class TestSession:
    async def test_commit_is_visible_and_rollback_is_not(self, db_session: AsyncSession,
                                                         db_engine: AsyncEngine) -> None:
        await db_session.execute(text("INSERT INTO materials (path) VALUES ('D:/m/committed.mp4')"))
        await db_session.commit()
        await db_session.execute(text("INSERT INTO materials (path) VALUES ('D:/m/rolled.mp4')"))
        await db_session.rollback()

        async with db_engine.connect() as conn:
            paths = set((await conn.execute(text("SELECT path FROM materials"))).scalars())
        assert "D:/m/committed.mp4" in paths
        assert "D:/m/rolled.mp4" not in paths

"""在临时 PostgreSQL 测量状态读取装配 200 次、缓存查询 100 次；不访问业务库。"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from xhs_agent.config import AppConfig
from xhs_agent.db import Base
from xhs_agent.db.models import ChatSession
from xhs_agent.schemas import ContextFact, HotspotClue
from xhs_agent.services import context_state, image_cache
from xhs_agent.services.chat import create_session


def distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {"count": len(values), "median_ms": round(median(values), 3),
            "p95_ms": round(ordered[math.ceil(len(values) * 0.95) - 1], 3),
            "max_ms": round(max(values), 3), "samples_ms": values}


async def run(dsn: str, output: Path) -> int:
    cfg = AppConfig()
    cfg.context.mode = "enforce"
    engine = create_async_engine(dsn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            sid = uuid.UUID((await create_session(session))["session_id"])
            row = await session.get(ChatSession, sid)
            state = dict(row.context_state)
            state["facts"] = [ContextFact(fact_id=f"f{i}", kind="constraint", value=f"第{i}条有效约束",
                scope="session", scope_id="", status="active", evidence=f"第{i}条有效约束",
                source_ref={"message_id": str(uuid.uuid4())}, created_version=1).to_dict() for i in range(41)]
            row.context_state = state
            await session.commit()
            clue = HotspotClue.from_dict({"hotspot_raw": "夜跑", "why_it_works": ["反差"],
                "mechanisms": [{"name": "反差"}], "elements": [{"type": "topic", "value": "夜跑",
                "weight": 1, "confidence": 1, "evidence": "夜跑"}]})
            identity = image_cache.build_identity(cfg, ["a" * 64])
            await image_cache.store(session, cfg, session_id=sid, identity=identity,
                                    payload={"raw_text": "夜跑", "clue": clue.to_dict()})
            state_times, cache_times = [], []
            for i in range(210):
                started = time.perf_counter()
                stored = await context_state.load(session, sid)
                rendered = context_state.state_prompt(stored)
                assert "第40条有效约束" in rendered
                elapsed = (time.perf_counter() - started) * 1000
                if i >= 10:
                    state_times.append(elapsed)
            for i in range(110):
                started = time.perf_counter()
                result = await image_cache.lookup(session, cfg, session_id=sid, identity=identity,
                                                  turn_id="benchmark", call_id=str(i))
                assert result.hit
                elapsed = (time.perf_counter() - started) * 1000
                if i >= 10:
                    cache_times.append(elapsed)
            report = {"scope": "temporary_postgres_local", "host": platform.platform(),
                "warmup_each": 10, "facts": 41, "threshold_ms": 100,
                "state_read_and_assemble": distribution(state_times),
                "cache_lookup_and_observation": distribution(cache_times)}
            report["passed"] = all(report[key]["p95_ms"] <= 100 for key in (
                "state_read_and_assemble", "cache_lookup_and_observation"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({key: value if not isinstance(value, dict) else
                              {k: v for k, v in value.items() if k != "samples_ms"}
                              for key, value in report.items()}, ensure_ascii=False))
            return 0 if report["passed"] else 1
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="runs/_e7b6_final/performance.json")
    args = parser.parse_args()
    with PostgresContainer("pgvector/pgvector:0.8.6-pg16", driver="asyncpg",
                           username="test", password="test", dbname="test") as postgres:
        return asyncio.run(run(postgres.get_connection_url(), Path(args.out)))


if __name__ == "__main__":
    raise SystemExit(main())

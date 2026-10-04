"""陈旧 run 对账的集成用例（S4.1）：真 Postgres 容器 + 假时间，不联网。

覆盖：只动 `running` 且超阈值的行、其余状态逐字段不变、阈值取 `task_time_limit_s × 2`、
边界（正好等于截止点不算陈旧）、幂等（跑第二次返回空）、worker 启动入口（同步外壳）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Run
from xhs_agent.services.runs import reconcile_stale_runs, stale_error
from xhs_agent.tasks import reconcile as reconcile_task
from xhs_agent.tasks.reconcile import stale_threshold_s

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

THRESHOLD = 1800
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, postgres_dsn: str) -> AppConfig:
    """容器 DSN + 60 秒硬超时（阈值 120 秒），不写仓库任何文件。"""
    path = tmp_path / "config.toml"
    path.write_text("[queue]\ntask_soft_time_limit_s = 30\ntask_time_limit_s = 60\n",
                    encoding="utf-8")
    loaded = load_config(str(path))
    loaded._env = EnvView({}, {"DATABASE_URL": postgres_dsn})
    return loaded


async def seed_run(session: AsyncSession, *, status: str,
                   started_at: datetime | None = None) -> str:
    """直接插一行 `runs`（提交掉，让其它连接也能看见）。"""
    run_id = (await session.execute(text(
        "INSERT INTO runs (job_id, status, started_at) VALUES (:job_id, :status, :started_at) "
        "RETURNING id::text"),
        {"job_id": str(uuid.uuid4()), "status": status, "started_at": started_at})).scalar_one()
    await session.commit()
    return str(run_id)


async def row_state(session: AsyncSession, run_id: str) -> tuple[str, str | None, bool]:
    """按列读回（不走 identity map），拿到的是数据库里的最新值。"""
    status, error, finished_at = (await session.execute(
        select(Run.status, Run.error, Run.finished_at)
        .where(Run.id == uuid.UUID(run_id)))).one()
    return str(status), (str(error) if error else None), finished_at is not None


class TestReconcileStaleRuns:
    async def test_marks_only_stale_running_rows(self, db_session: AsyncSession) -> None:
        stale = await seed_run(db_session, status="running",
                               started_at=NOW - timedelta(seconds=THRESHOLD + 1))
        fresh = await seed_run(db_session, status="running",
                               started_at=NOW - timedelta(seconds=THRESHOLD - 1))
        queued = await seed_run(db_session, status="queued")
        done = await seed_run(db_session, status="succeeded",
                              started_at=NOW - timedelta(days=1))
        orphan = await seed_run(db_session, status="running", started_at=None)

        rows = await reconcile_stale_runs(db_session, threshold_s=THRESHOLD, now=NOW)

        assert [row["run_id"] for row in rows] == [stale]
        assert rows[0]["job_id"] and rows[0]["started_at"]
        status, error, finished = await row_state(db_session, stale)
        assert (status, error, finished) == ("failed", stale_error(THRESHOLD), True)
        for untouched in (fresh, queued, done, orphan):
            status, error, finished = await row_state(db_session, untouched)
            assert status != "failed" and error is None and finished is False

    async def test_boundary_at_cutoff_is_not_stale(self, db_session: AsyncSession) -> None:
        """正好等于截止点不算陈旧（`started_at < cutoff` 严格小于）。"""
        edge = await seed_run(db_session, status="running", started_at=NOW - timedelta(seconds=THRESHOLD))
        assert await reconcile_stale_runs(db_session, threshold_s=THRESHOLD, now=NOW) == []
        assert (await row_state(db_session, edge))[0] == "running"

    async def test_second_pass_is_idempotent(self, db_session: AsyncSession) -> None:
        await seed_run(db_session, status="running",
                       started_at=NOW - timedelta(seconds=THRESHOLD + 60))
        first = await reconcile_stale_runs(db_session, threshold_s=THRESHOLD, now=NOW)
        second = await reconcile_stale_runs(db_session, threshold_s=THRESHOLD, now=NOW)
        assert len(first) == 1 and second == []

    async def test_default_threshold_multiplies_hard_limit(self, cfg: AppConfig) -> None:
        assert stale_threshold_s(cfg) == 120          # 60 × 2（见 STALE_MULTIPLIER）


class TestWorkerReadyEntry:
    async def test_sync_entry_marks_stale_rows(self, db_session: AsyncSession,
                                               cfg: AppConfig) -> None:
        """worker 启动入口（同步外壳）能真连库、真标记——`asyncio.run` 放到线程里跑。"""
        stale = await seed_run(
            db_session, status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=stale_threshold_s(cfg) + 5))

        rows = await asyncio.to_thread(reconcile_task.run_reconcile, cfg)

        assert [row["run_id"] for row in rows] == [stale]
        status, error, finished = await row_state(db_session, stale)
        assert (status, finished) == ("failed", True)
        assert "陈旧运行对账" in str(error)

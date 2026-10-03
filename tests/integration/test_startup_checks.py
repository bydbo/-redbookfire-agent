"""启动前置检查的集成用例（S3.8）：在真容器 Postgres + Redis 上跑第 3–7 步。

单元用例只覆盖"连不上 / 缺产物"这些能离线构造的分支；"扩展在位、迁移到位"必须在真库上验。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.probe import (
    E_DB_EXTENSION,
    E_DB_MIGRATION,
    check_extensions,
    run_startup_checks,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cfg(tmp_path: Path, postgres_dsn: str, redis_url: str) -> AppConfig:
    """第 1–2 步能过的配置：容器 DSN + 容器 Redis；serve=false 免得被第 7 步挡住。"""
    path = tmp_path / "config.toml"
    path.write_text("[embedding]\nenabled = false\n[frontend]\nserve = false\n",
                    encoding="utf-8")
    loaded = load_config(str(path))
    loaded._env = EnvView({}, {"DEEPSEEK_API_KEY": "sk-x", "DATABASE_URL": postgres_dsn,
                               "REDIS_URL": redis_url})
    return loaded


@pytest.fixture
def alembic_config(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    return Config(str(PROJECT_ROOT / "alembic.ini"))


async def _upgrade(config: Config) -> None:
    """env.py 的在线模式内部用 `asyncio.run`，丢到线程里跑（同 test_migrations）。"""
    await asyncio.to_thread(command.upgrade, config, "head")


class TestStartupChecksAgainstRealDependencies:
    async def test_clean_database_reports_missing_migration(self, cfg: AppConfig) -> None:
        """干净库（还没迁移）→ 第 5 步失败、退出码 3，并给出能照做的修复提示。"""
        report = await run_startup_checks(cfg)

        assert [problem.code for problem in report.problems] == [E_DB_MIGRATION]
        assert report.problems[0].step == 5
        assert report.exit_code == 3
        assert "alembic upgrade head" in report.problems[0].fix

    async def test_all_steps_pass_after_migrating(self, cfg: AppConfig,
                                                  alembic_config: Config) -> None:
        await _upgrade(alembic_config)

        report = await run_startup_checks(cfg)

        assert report.problems == []
        assert report.exit_code == 0

    async def test_missing_extension_is_reported(self, cfg: AppConfig,
                                                 db_engine: AsyncEngine) -> None:
        """缺 pg_trgm → 第 4 步失败并点名缺哪个扩展（干净库上才 drop 得掉）。"""
        async with db_engine.begin() as conn:
            await conn.execute(text("DROP EXTENSION IF EXISTS pg_trgm"))

        problem = await check_extensions(db_engine)

        assert problem is not None
        assert problem.code == E_DB_EXTENSION
        assert problem.step == 4 and problem.kind == "dependency"
        assert "pg_trgm" in problem.message

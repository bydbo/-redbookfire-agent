"""数据库装配的离线单测（不连库）：DSN 校验与池参数接线。

真正的连通与表结构验证在 `tests/integration/test_db_schema.py`（需要 Docker）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xhs_agent.config import ConfigError, load_config
from xhs_agent.db import create_engine_from_config, create_session_factory, database_url

DSN = "postgresql+asyncpg://xhs:xhs@localhost:5432/xhs"


def write_config(tmp_path: Path, body: str = "") -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestDatabaseUrl:
    def test_reads_env_value(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("DATABASE_URL", DSN)
        assert database_url(load_config(write_config(tmp_path))) == DSN

    def test_missing_value_raises_with_fix_hint(self, tmp_path, clean_contract_env):
        with pytest.raises(ConfigError) as info:
            database_url(load_config(write_config(tmp_path)))
        assert "DATABASE_URL" in str(info.value)
        assert "config/.env" in info.value.fix

    def test_wrong_driver_prefix_is_rejected(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("DATABASE_URL", "postgresql+psycopg2://xhs:xhs@localhost/xhs")
        with pytest.raises(ConfigError) as info:
            database_url(load_config(write_config(tmp_path)))
        assert "postgresql+asyncpg://" in str(info.value)


class TestEngine:
    def test_pool_settings_come_from_database_section(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("DATABASE_URL", DSN)
        cfg = load_config(write_config(
            tmp_path, "[database]\npool_size = 7\nmax_overflow = 3\necho = true\n"))
        engine = create_engine_from_config(cfg)
        try:
            status = engine.pool.status()
            assert "Pool size: 7" in status
            assert engine.echo is True
            assert engine.pool._max_overflow == 3
        finally:
            asyncio.run(engine.dispose())

    def test_engine_creation_does_not_open_a_connection(self, tmp_path, clean_contract_env):
        # 指向一个必然连不上的端口：能构造成功，就说明构造期没有真的连库（惰性）
        clean_contract_env.setenv("DATABASE_URL", "postgresql+asyncpg://xhs:xhs@127.0.0.1:1/xhs")
        engine = create_engine_from_config(load_config(write_config(tmp_path)))
        try:
            assert engine.url.port == 1
        finally:
            asyncio.run(engine.dispose())

    def test_session_factory_returns_sessions(self, tmp_path, clean_contract_env):
        clean_contract_env.setenv("DATABASE_URL", DSN)
        engine = create_engine_from_config(load_config(write_config(tmp_path)))
        session = create_session_factory(engine)()
        try:
            assert session.is_active
        finally:
            asyncio.run(session.close())
            asyncio.run(engine.dispose())

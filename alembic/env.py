"""Alembic 迁移环境（async）。

用途：`alembic upgrade / downgrade / check` 的执行环境。
输入：连接串统一从 `AppConfig` 取（`config/.env` → 环境变量 `DATABASE_URL`）；
      **不在 `alembic.ini` 里写 URL**，避免"配置文件一处、`.env` 又一处"的漂移。
输出：对目标库执行迁移；`target_metadata` 指向 ORM 的 `Base.metadata`，供 autogenerate 比对。
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from xhs_agent.config import ConfigError, load_config
from xhs_agent.db import Base
from xhs_agent.db.session import database_url

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """连接串只从 AppConfig 取；缺配置时给出可照做的提示（不降级）。"""
    try:
        return database_url(load_config())
    except ConfigError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}\n修复：{exc.fix}") from exc


config.set_main_option("sqlalchemy.url", _database_url())


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

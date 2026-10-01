"""集成测试基座（S2.9）：真实 Postgres / Redis 容器 + 异步 engine/session + 每用例清库。

约定（见 `AGENTS.md` 第 3 节）：

- 默认 `uv run pytest` **不跑**本目录（`addopts` 排除了 `integration` 标记）；
- 集成用例显式触发：`uv run pytest -m integration`；
- 需要可用的 Docker：连不上时**直接失败并说明原因**，不静默跳过（项目不做降级）。

容器镜像与 `docker-compose.yml` 保持一致，Postgres 复用同一份扩展初始化脚本
`docker/postgres/init/01-extensions.sql`，保证 compose 与集成测试的扩展口径不漂移。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
import redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

import docker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
REDIS_IMAGE = "redis:7.4.11-alpine"
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"
INIT_SQL = INIT_DIR / "01-extensions.sql"

DB_USER = "xhs"
DB_PASSWORD = "xhs"
DB_NAME = "xhs"


def _init_statements() -> list[str]:
    """把初始化 SQL 拆成单条语句——asyncpg 不允许一次执行多条。"""
    body = "\n".join(line for line in INIT_SQL.read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("--"))
    return [statement.strip() for statement in body.split(";") if statement.strip()]


@pytest.fixture(scope="session", autouse=True)
def require_docker_daemon() -> None:
    """整个集成测试会话只探测一次 Docker；不可用就终止并给出可照做的提示。"""
    try:
        docker.from_env().ping()
    except Exception as exc:  # 连接类异常若干种，统一折成一条中文提示
        pytest.exit(
            "集成测试需要可用的 Docker：请先启动 Docker Desktop，再重试 "
            f"`uv run pytest -m integration`（原始错误：{type(exc).__name__}: {exc}）",
            returncode=1,
        )


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        POSTGRES_IMAGE, driver="asyncpg", username=DB_USER, password=DB_PASSWORD,
        dbname=DB_NAME,
    ).with_volume_mapping(str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
    with container:
        yield container


@pytest.fixture(scope="session")
def postgres_dsn(postgres_container: PostgresContainer) -> str:
    """SQLAlchemy async 用的 DSN（`postgresql+asyncpg://…`）。"""
    return postgres_container.get_connection_url()


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container: RedisContainer) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture
def redis_client(redis_container: RedisContainer) -> Iterator[redis.Redis]:
    client = redis_container.get_client()
    try:
        yield client
    finally:
        client.close()


@pytest_asyncio.fixture(scope="session")
async def db_engine(postgres_dsn: str) -> AsyncIterator[AsyncEngine]:
    """session 级异步引擎：整场集成测试共用一个连接池（NullPool，容器会话结束即释放）。"""
    engine = create_async_engine(postgres_dsn, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def reset_database(db_engine: AsyncEngine) -> None:
    """每个集成用例前把 `public` schema 清空并重建扩展，保证用例之间互不污染。

    注意：`DROP SCHEMA public CASCADE` 会连带删掉装在 public 里的扩展，
    所以随后逐条重放 `docker/postgres/init/01-extensions.sql`（S2.3 的 Alembic 用例
    可以在这个干净库上直接 `upgrade head`）。
    """
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        for statement in _init_statements():
            await conn.execute(text(statement))


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine, reset_database: None) -> AsyncIterator[AsyncSession]:
    """函数级异步会话；依赖 `reset_database` 以确保「先清库、后开会话」的顺序。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """安全网：给本目录下的用例自动补 `integration` 标记。

    忘记写 `pytestmark` 时，默认的单测运行也不会误触 Docker。
    """
    here = Path(__file__).resolve().parent
    for item in items:
        if here in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.integration)

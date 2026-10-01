"""集成测试基座冒烟（S2.9）：容器连通、扩展口径、异步 engine、Redis、清库隔离。

这里只证明"基座可用"，不含业务断言——业务集成用例从 S2.2 起陆续加进来。
"""

from __future__ import annotations

import pytest
import redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.integration


def _version_tuple(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.split(".")[:2])


class TestPostgres:
    @pytest.mark.asyncio
    async def test_async_engine_round_trip(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_extensions_match_contract(self, db_engine: AsyncEngine) -> None:
        async with db_engine.connect() as conn:
            rows = dict((await conn.execute(text(
                "SELECT extname, extversion FROM pg_extension "
                "WHERE extname IN ('vector', 'pg_trgm')"
            ))).all())
        assert set(rows) == {"vector", "pg_trgm"}
        assert _version_tuple(rows["vector"]) >= (0, 5)

    @pytest.mark.asyncio
    async def test_vector_type_is_usable(self, db_session: AsyncSession) -> None:
        value = await db_session.scalar(text("SELECT '[1,2,3]'::vector"))
        assert value is not None


class TestRedis:
    def test_ping(self, redis_client: redis.Redis) -> None:
        assert redis_client.ping() is True

    def test_url_shape_matches_config_contract(self, redis_url: str) -> None:
        assert redis_url.startswith("redis://")


class TestIsolation:
    """这两条用例有先后依赖：第二条证明第一条建的表被清库夹具清掉了。"""

    @pytest.mark.asyncio
    async def test_creates_probe_table(self, db_session: AsyncSession) -> None:
        await db_session.execute(text("CREATE TABLE isolation_probe (id int)"))
        await db_session.commit()
        assert await db_session.scalar(text(
            "SELECT to_regclass('public.isolation_probe')")) is not None

    @pytest.mark.asyncio
    async def test_probe_table_is_gone_after_reset(self, db_session: AsyncSession) -> None:
        assert await db_session.scalar(text(
            "SELECT to_regclass('public.isolation_probe')")) is None

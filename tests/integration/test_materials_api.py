"""素材库查询的集成用例（S6.1）：真 Postgres 容器 + 临时素材目录。

覆盖列表的排序 / 分页 / 关键词 / 类型 / 来源 / 主题目录筛选、主题计数与单条详情。
这里只用路径当基准（不落真实文件）——真实扫描与入库归 S6.4 的用例。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Material as MaterialRow
from xhs_agent.services.materials import list_materials, load_material

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# (相对路径, 类型, 标题, 标签, 来源, indexed_at 相对 BASE 的分钟数)
SEED = [
    ("运动/挥拍.mp4", "video", "球场挥拍", ["羽毛球", "球场"], "sidecar", 3),
    ("运动/夜里/夜跑.mp4", "video", "夜跑打卡", ["夜跑"], "vision", 2),
    ("美食/火锅.jpg", "image", "火锅特写", ["美食", "火锅"], "sidecar", 1),
    ("根目录.mp4", "video", "根目录素材", ["根"], "filename", 4),
    ("50%off.jpg", "image", "50% 折扣海报", ["促销"], "legacy", 5),
]


def write_config(tmp_path: Path) -> AppConfig:
    materials = tmp_path / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{materials.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return write_config(tmp_path)


@pytest.fixture
def client(db_engine: AsyncEngine, cfg: AppConfig,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


async def seed(session: AsyncSession, cfg: AppConfig) -> dict[str, str]:
    """插入固定 5 行（其中一条带爆点要素），返回相对路径 → id 字符串。"""
    created: dict[str, MaterialRow] = {}
    for rel, kind, title, tags, source, minutes in SEED:
        row = MaterialRow(
            path=str(Path(cfg.materials_dir()) / rel), type=kind, title=title,
            description=f"{title} 的描述", tags=list(tags), source=source,
            elements=[{"type": "topic", "value": title, "weight": 0.8,
                       "confidence": 0.7, "evidence": "旁车"}] if rel == "运动/挥拍.mp4" else [],
            keyframes=["runs/_index/keyframes/x/kf_0.jpg"] if kind == "video" else [],
            size_bytes=1024, duration_s=12.5 if kind == "video" else 0,
            indexed_at=BASE_TIME + timedelta(minutes=minutes),
        )
        session.add(row)
        created[rel] = row
    await session.commit()
    return {rel: str(row.id) for rel, row in created.items()}


def titles(payload: dict[str, Any]) -> list[str]:
    return [item["title"] for item in payload["items"]]


class TestListing:
    async def test_orders_by_indexed_at_desc(self, db_session: AsyncSession,
                                             cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        payload = await list_materials(db_session, cfg)
        assert payload["total"] == 5
        assert payload["limit"] == 24 and payload["offset"] == 0
        assert titles(payload) == ["50% 折扣海报", "根目录素材", "球场挥拍", "夜跑打卡", "火锅特写"]

    async def test_pagination_keeps_total(self, db_session: AsyncSession,
                                          cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        first = await list_materials(db_session, cfg, limit=2)
        second = await list_materials(db_session, cfg, limit=2, offset=2)
        last = await list_materials(db_session, cfg, limit=2, offset=4)
        assert len(first["items"]) == 2 and len(second["items"]) == 2
        assert len(last["items"]) == 1
        assert first["total"] == second["total"] == 5

    async def test_keyword_hits_title_or_tag(self, db_session: AsyncSession,
                                             cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        by_title = await list_materials(db_session, cfg, q="火锅")
        by_tag = await list_materials(db_session, cfg, q="羽毛球")
        blank = await list_materials(db_session, cfg, q="   ")
        assert titles(by_title) == ["火锅特写"]
        assert titles(by_tag) == ["球场挥拍"]
        assert blank["total"] == 5          # 空白关键词 = 不筛选

    async def test_keyword_escapes_like_wildcards(self, db_session: AsyncSession,
                                                  cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        percent = await list_materials(db_session, cfg, q="50%")
        lone = await list_materials(db_session, cfg, q="%")
        underscore = await list_materials(db_session, cfg, q="_")
        assert titles(percent) == ["50% 折扣海报"]
        assert titles(lone) == ["50% 折扣海报"]     # `%` 是字面量，不是"匹配全部"
        assert underscore["total"] == 0

    async def test_type_and_source_filters(self, db_session: AsyncSession,
                                           cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        images = await list_materials(db_session, cfg, type="image")
        sidecar = await list_materials(db_session, cfg, source="sidecar")
        combined = await list_materials(db_session, cfg, type="video", source="vision")
        assert set(titles(images)) == {"50% 折扣海报", "火锅特写"}
        assert set(titles(sidecar)) == {"球场挥拍", "火锅特写"}
        assert titles(combined) == ["夜跑打卡"]

    async def test_dir_filter_includes_subdirectories(self, db_session: AsyncSession,
                                                      cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        root = await list_materials(db_session, cfg, dir="")
        sport = await list_materials(db_session, cfg, dir="运动")
        nested = await list_materials(db_session, cfg, dir="运动/夜里")
        assert set(titles(root)) == {"50% 折扣海报", "根目录素材"}
        assert set(titles(sport)) == {"夜跑打卡", "球场挥拍"}
        assert titles(nested) == ["夜跑打卡"]

    async def test_dirs_aggregation_ignores_dir_filter(self, db_session: AsyncSession,
                                                       cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        payload = await list_materials(db_session, cfg, dir="运动")
        assert payload["dirs"] == [{"path": "美食", "count": 1}, {"path": "运动", "count": 2}]
        only_images = await list_materials(db_session, cfg, type="image")
        assert only_images["dirs"] == [{"path": "美食", "count": 1}]     # 根目录的不计入


class TestDetail:
    async def test_detail_returns_elements(self, db_session: AsyncSession,
                                           cfg: AppConfig) -> None:
        ids = await seed(db_session, cfg)
        detail = await load_material(db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]))
        assert detail is not None
        assert detail["dir"] == "运动"
        assert detail["source"] == "sidecar"
        assert detail["elements"][0]["value"] == "球场挥拍"
        assert detail["keyframes"] == ["runs/_index/keyframes/x/kf_0.jpg"]

    async def test_unknown_id_returns_none(self, db_session: AsyncSession,
                                          cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        assert await load_material(db_session, cfg, uuid.uuid4()) is None


class TestEndpoint:
    async def test_list_endpoint_shape(self, client: TestClient, db_session: AsyncSession,
                                       cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        response = client.get("/api/materials?limit=2&type=video")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"items", "total", "limit", "offset", "dirs"}
        assert body["total"] == 3 and body["limit"] == 2
        assert set(body["items"][0]) == {
            "id", "path", "type", "title", "description", "tags", "duration_s", "width",
            "height", "has_audio", "size_bytes", "source", "keyframes", "indexed_at", "dir",
        }

    async def test_detail_endpoint(self, client: TestClient, db_session: AsyncSession,
                                   cfg: AppConfig) -> None:
        ids = await seed(db_session, cfg)
        ok = client.get(f"/api/materials/{ids['运动/挥拍.mp4']}")
        assert ok.status_code == 200, ok.text
        assert ok.json()["elements"][0]["type"] == "topic"

        missing = client.get(f"/api/materials/{uuid.uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"

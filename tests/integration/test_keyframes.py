"""取帧接口的集成用例（S5.4）：`GET /api/materials/{material_id}/keyframes/{index}`。

覆盖：正常返回（字节一致 + image/jpeg）、素材不存在、下标越界、登记路径越出
关键帧缓存目录（路径穿越防护）、目录内但文件缺失、非法 uuid 按 404 处理
（契约该路径只列 404/503，不引入 422）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Material

pytestmark = [pytest.mark.integration]

JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def write_config(tmp_path: Path) -> AppConfig:
    """临时配置：三个路径全指 tmp（index_dir 显式指 tmp，不碰仓库的关键帧缓存）。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "index").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redis_url: str) -> AppConfig:
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
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
    app.dependency_overrides[get_dispatcher] = lambda: None
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


async def _add_material(db_engine: AsyncEngine, keyframes: list[str]) -> uuid.UUID:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        material = Material(path="D:/materials/挥拍.mp4", type="video", title="挥拍",
                            description="", tags=[], elements=[], source="sidecar",
                            width=1080, height=1920, size_bytes=1024,
                            keyframes=keyframes)
        session.add(material)
        await session.commit()
        return material.id


def _seed_material(db_engine: AsyncEngine, keyframes: list[str]) -> uuid.UUID:
    return asyncio.run(_add_material(db_engine, keyframes))


def test_keyframe_happy_path(client: TestClient, db_engine: AsyncEngine, cfg: AppConfig) -> None:
    frame_dir = Path(cfg.index_dir(), "keyframes", "m_test")
    frame_dir.mkdir(parents=True)
    frame = frame_dir / "kf_0.jpg"
    frame.write_bytes(JPEG_BYTES)
    material_id = _seed_material(db_engine, [str(frame)])

    response = client.get(f"/api/materials/{material_id}/keyframes/0")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == JPEG_BYTES


def test_keyframe_material_not_found(client: TestClient) -> None:
    response = client.get(f"/api/materials/{uuid.uuid4()}/keyframes/0")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_keyframe_index_out_of_range(client: TestClient, db_engine: AsyncEngine,
                                     cfg: AppConfig) -> None:
    frame_dir = Path(cfg.index_dir(), "keyframes", "m_x")
    frame_dir.mkdir(parents=True)
    (frame_dir / "kf_0.jpg").write_bytes(JPEG_BYTES)
    material_id = _seed_material(db_engine, [str(frame_dir / "kf_0.jpg")])

    assert client.get(f"/api/materials/{material_id}/keyframes/1").status_code == 404
    assert client.get(f"/api/materials/{material_id}/keyframes/-1").status_code == 404


def test_keyframe_path_outside_cache_dir_is_404(client: TestClient, db_engine: AsyncEngine,
                                                tmp_path: Path) -> None:
    """登记的路径即便真实存在，只要越出关键帧缓存目录就必须 404（路径穿越防护）。"""
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG_BYTES)
    material_id = _seed_material(db_engine, [str(outside)])

    response = client.get(f"/api/materials/{material_id}/keyframes/0")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_keyframe_missing_file_is_404(client: TestClient, db_engine: AsyncEngine,
                                      cfg: AppConfig) -> None:
    """目录内但文件已被清理：同样 404，与越界不区分原因。"""
    frame_dir = Path(cfg.index_dir(), "keyframes", "m_gone")
    frame_dir.mkdir(parents=True)
    material_id = _seed_material(db_engine, [str(frame_dir / "kf_0.jpg")])

    assert client.get(f"/api/materials/{material_id}/keyframes/0").status_code == 404


def test_keyframe_invalid_uuid_is_404(client: TestClient) -> None:
    """契约该路径只列 404/503：非法 uuid 按资源不存在处理，不引入 422。"""
    response = client.get("/api/materials/not-a-uuid/keyframes/0")
    assert response.status_code == 404

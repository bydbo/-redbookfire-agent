"""扫描 / 上传 / 任务状态的集成用例（S6.4）：真 Postgres + 真 Redis 容器 + 临时素材目录。

上传走真接口（真落盘），索引任务用真库真文件 + 假向量客户端跑通，任务状态在**真 Redis**
的结果后端上读写（这是 ADR 0014 的核心路径，单测里只能替身）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import CeleryDispatcher, get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Material as MaterialRow
from xhs_agent.services.materials import list_materials
from xhs_agent.tasks.indexing import run_index, run_locked
from xhs_agent.tools.embedding import EmbeddingResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class FakeDispatcher:
    """假投递器：只记账（真投递由 TestTaskStateOnRealRedis 覆盖）。"""

    def __init__(self) -> None:
        self.index_calls = 0

    async def enqueue(self, job_id: str) -> None:
        raise AssertionError("素材接口不该投分析任务")

    async def enqueue_index(self, *, force_paths: list[str] | None = None) -> str:
        self.index_calls += 1
        return "task-1"

    async def task_state(self, task_id: str) -> tuple[str, Any]:
        return ("PENDING", None)


class FakeEmbedder:
    """假向量客户端：固定返回单位向量，不联网。"""

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0] + [0.0] * 1023 for _ in texts],
                               model="fake", prompt_tokens=1, attempts=1)


def write_config(tmp_path: Path) -> AppConfig:
    materials = tmp_path / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{materials.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[frontend]\nserve = false\n",
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
def task_cfg(cfg: AppConfig, postgres_dsn: str, redis_url: str) -> AppConfig:
    """任务自己建 engine / Celery app 时用：DSN 与 broker 指向容器（与 cfg 同一个对象）。"""
    cfg._env = EnvView({}, {"DATABASE_URL": postgres_dsn, "REDIS_URL": redis_url})
    return cfg


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def client(db_engine: AsyncEngine, cfg: AppConfig,
           dispatcher: FakeDispatcher) -> Iterator[TestClient]:
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestUploadAndIndex:
    async def test_upload_lands_in_month_dir_then_index_lists_it(
            self, client: TestClient, db_session: AsyncSession, task_cfg: AppConfig,
            cfg: AppConfig, dispatcher: FakeDispatcher) -> None:
        month = datetime.now().strftime("%Y-%m")

        response = client.post("/api/materials/uploads",
                               files={"file": ("新片.mp4", b"fake-video-bytes", "video/mp4")})

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["task_id"] == "task-1" and body["name"] == "新片.mp4"
        assert body["path"] == f"{month}/新片.mp4"
        landed = Path(cfg.materials_dir()) / month / "新片.mp4"
        assert landed.read_bytes() == b"fake-video-bytes"
        assert list(Path(cfg.materials_dir()).rglob("*.part")) == []
        assert dispatcher.index_calls == 1

        report = await run_index(task_cfg, embedder=FakeEmbedder())

        assert report["added"] == 1 and report["embedded"] == 1
        listing = await list_materials(db_session, task_cfg)
        item = next(entry for entry in listing["items"] if entry["path"].endswith("新片.mp4"))
        assert item["dir"] == month and item["type"] == "video"
        assert listing["dirs"] == [{"path": month, "count": 1}]

    async def test_second_upload_with_same_name_gets_suffix(
            self, client: TestClient, cfg: AppConfig) -> None:
        first = client.post("/api/materials/uploads",
                            files={"file": ("同名.mp4", b"a", "video/mp4")})
        second = client.post("/api/materials/uploads",
                             files={"file": ("同名.mp4", b"b", "video/mp4")})

        assert first.status_code == 202 and second.status_code == 202
        assert first.json()["name"] == "同名.mp4"
        assert second.json()["name"] == "同名-2.mp4"
        assert (Path(cfg.materials_dir()) / second.json()["path"]).read_bytes() == b"b"

    async def test_scan_endpoint_accepts_task(self, client: TestClient) -> None:
        response = client.post("/api/materials/scan")
        assert response.status_code == 202
        assert response.json()["task_id"] == "task-1"


class TestTaskStateOnRealRedis:
    async def test_dispatcher_reads_backend(self, task_cfg: AppConfig) -> None:
        dispatcher = CeleryDispatcher(task_cfg)
        app = dispatcher.app()
        app.backend.store_result(
            "task-succeeded",
            {"scanned": 2, "added": 1, "summary": "素材同步：扫描 2、新增 1；嵌入 1"},
            "SUCCESS")

        state, info = await dispatcher.task_state("task-succeeded")

        assert state == "SUCCESS"
        assert info["added"] == 1 and "素材同步" in info["summary"]

    async def test_unknown_task_is_pending(self, task_cfg: AppConfig) -> None:
        state, info = await CeleryDispatcher(task_cfg).task_state("never-sent")
        assert state == "PENDING" and info is None

    async def test_endpoint_reports_success(self, db_engine: AsyncEngine,
                                            task_cfg: AppConfig) -> None:
        """接口层读的就是真后端的值（契约 `MaterialTask` 形状）。"""
        real = CeleryDispatcher(task_cfg)
        real.app().backend.store_result(
            "task-x", {"added": 3, "summary": "素材同步：扫描 3、新增 3"}, "SUCCESS")

        app = create_app(check_startup=False)
        app.dependency_overrides[get_config] = lambda: task_cfg
        app.dependency_overrides[get_dispatcher] = lambda: real

        async def _session() -> AsyncIterator[AsyncSession]:
            factory = async_sessionmaker(db_engine, expire_on_commit=False)
            async with factory() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/materials/tasks/task-x")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "SUCCESS"
        assert body["result"]["added"] == 3
        assert body["step"] is None

    async def test_failed_task_reports_reason(self, task_cfg: AppConfig) -> None:
        dispatcher = CeleryDispatcher(task_cfg)
        dispatcher.app().backend.mark_as_failure(
            "task-failed", RuntimeError("素材目录不存在"), traceback=None)

        state, info = await dispatcher.task_state("task-failed")

        assert state == "FAILURE"
        assert "素材目录不存在" in str(info)


async def test_env_has_redis_dsn(task_cfg: AppConfig) -> None:
    """守住上面那些用例的前提：任务配置真的指向容器 Redis 与数据库。"""
    assert (task_cfg.env_view.get("REDIS_URL") or "").startswith("redis://")
    assert (task_cfg.env_view.get("DATABASE_URL")
            or "").startswith("postgresql+asyncpg://")


class TestConcurrentIndexing:
    """S6.5 冒烟抓到的并发问题：多个上传各投一个索引任务，撞在一起就重复打标 + 插入冲突。"""

    async def test_locked_runs_are_serialized(self, task_cfg: AppConfig,
                                             db_session: AsyncSession,
                                             cfg: AppConfig) -> None:
        touch = Path(cfg.materials_dir()) / "运动" / "并发新片.mp4"
        touch.parent.mkdir(parents=True, exist_ok=True)
        touch.write_bytes(b"fake-video")

        reports = await asyncio.gather(
            run_locked(task_cfg, embedder=FakeEmbedder()),
            run_locked(task_cfg, embedder=FakeEmbedder()),
        )

        assert all(report["scanned"] >= 1 for report in reports)
        rows = (await db_session.execute(
            select(MaterialRow).where(MaterialRow.path == str(touch)))).scalars().all()
        assert len(rows) == 1                       # 只入库一次
        assert sum(report["added"] for report in reports) == 1   # 第二个任务看到"没变化"

    async def test_unlocked_runs_still_do_not_crash(self, task_cfg: AppConfig,
                                                    db_session: AsyncSession,
                                                    cfg: AppConfig) -> None:
        """即使绕过锁（分析与索引同时做新鲜度同步），幂等插入也不该报唯一键冲突。"""
        touch = Path(cfg.materials_dir()) / "运动" / "无锁并发.mp4"
        touch.parent.mkdir(parents=True, exist_ok=True)
        touch.write_bytes(b"fake-video")

        reports = await asyncio.gather(
            run_index(task_cfg, embedder=FakeEmbedder()),
            run_index(task_cfg, embedder=FakeEmbedder()),
        )

        assert all(report["added"] >= 1 for report in reports)
        rows = (await db_session.execute(
            select(MaterialRow).where(MaterialRow.path == str(touch)))).scalars().all()
        assert len(rows) == 1

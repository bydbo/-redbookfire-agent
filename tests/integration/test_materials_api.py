"""素材库的集成用例（S6.1 查询 + S6.2 编辑 + S6.3 回收站与索引任务）。

真 Postgres 容器 + 临时素材目录。查询部分只用路径当基准（不落真实文件）；编辑与回收站要真文件，
因为改动必须同时落到磁盘与数据库两边；索引任务用真库、真文件 + 假向量客户端（不联网）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from celery.result import AsyncResult
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import CeleryDispatcher, get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Hotspot, Run, RunHotspot, RunMatch
from xhs_agent.db.models import Material as MaterialRow
from xhs_agent.services.materials import (
    list_materials,
    list_trash,
    load_material,
    purge_trash,
    restore_from_trash,
    trash_material,
    trash_root,
    update_material,
)
from xhs_agent.tasks.celery_app import build_celery_app
from xhs_agent.tasks.indexing import INDEX_TASK_NAME, register_index_task, run_index
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.materials import parse_sidecar, sidecar_path

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


class FakeIndexDispatcher:
    """假投递器：素材接口只该投索引任务，记账后返回固定 task_id。"""

    def __init__(self) -> None:
        self.index_calls: list[list[str] | None] = []

    async def enqueue(self, job_id: str) -> None:
        raise AssertionError("素材接口不该投分析任务")

    async def enqueue_index(self, *, force_paths: list[str] | None = None) -> str:
        self.index_calls.append(force_paths)
        return "task-1"


class FakeEmbedder:
    """假向量客户端：固定返回一个单位向量，不联网。"""

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
def task_cfg(cfg: AppConfig, postgres_dsn: str, redis_url: str) -> AppConfig:
    """给「任务自己建 engine / Celery app」的用例用：DSN 与 broker 指向容器。

    与 `cfg` 是**同一个对象**（就地补环境变量）——用例同时要它俩时拿到的配置一致。
    """
    cfg._env = EnvView({}, {"DATABASE_URL": postgres_dsn, "REDIS_URL": redis_url})
    return cfg


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
    app.dependency_overrides[get_dispatcher] = lambda: FakeIndexDispatcher()
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


def touch(relative: str, cfg: AppConfig, data: bytes = b"fake-media") -> str:
    """在临时素材目录里放一个真文件（编辑要写旁车，必须有真实路径）。"""
    path = Path(cfg.materials_dir()) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


async def reload_row(session: AsyncSession, material_id: str) -> MaterialRow:
    return (await session.execute(
        select(MaterialRow).where(MaterialRow.id == uuid.UUID(material_id)))).scalar_one()


class TestEdit:
    async def test_updates_db_and_writes_sidecar(self, db_session: AsyncSession,
                                                 cfg: AppConfig) -> None:
        media = touch("运动/挥拍.mp4", cfg)
        ids = await seed(db_session, cfg)

        detail = await update_material(
            db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]),
            title="  球场挥拍（改）  ", tags=[" 羽毛球 ", "#球场", "球场", ""],
            description="户外球场挥拍\n第二行")

        assert detail is not None
        assert detail["title"] == "球场挥拍（改）"        # 去空白
        assert detail["tags"] == ["羽毛球", "球场"]        # 去 `#` + 去重
        assert detail["description"] == "户外球场挥拍 第二行"   # 折成单行
        assert detail["source"] == "sidecar"              # 人工确认过

        row = await reload_row(db_session, ids["运动/挥拍.mp4"])
        assert (row.title, row.tags, row.description, row.source) == (
            "球场挥拍（改）", ["羽毛球", "球场"], "户外球场挥拍 第二行", "sidecar")

        written = sidecar_path(media)
        assert written == media + ".txt"
        assert parse_sidecar(written) == {"title": "球场挥拍（改）", "tags": ["羽毛球", "球场"],
                                          "description": "户外球场挥拍 第二行"}

    async def test_unchanged_values_do_not_write_file(self, db_session: AsyncSession,
                                                      cfg: AppConfig) -> None:
        media = touch("运动/夜里/夜跑.mp4", cfg)
        ids = await seed(db_session, cfg)

        detail = await update_material(db_session, cfg, uuid.UUID(ids["运动/夜里/夜跑.mp4"]),
                                       title="夜跑打卡")

        assert detail is not None and detail["title"] == "夜跑打卡"
        assert not os.path.exists(media + ".txt")
        # 没改动就不该顺手把视觉打标"洗"成人工来源
        assert (await reload_row(db_session, ids["运动/夜里/夜跑.mp4"])).source == "vision"

    async def test_existing_md_sidecar_is_kept(self, db_session: AsyncSession,
                                               cfg: AppConfig) -> None:
        media = touch("运动/挥拍.mp4", cfg)
        Path(media + ".md").write_text("标题: 手工写的\n", encoding="utf-8")
        ids = await seed(db_session, cfg)

        await update_material(db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]), title="新标题")

        assert os.path.isfile(media + ".md")           # 原文件保留
        assert sidecar_path(media) == media + ".txt"   # 但 .txt 优先
        assert parse_sidecar(media + ".txt")["title"] == "新标题"

    async def test_missing_media_updates_db_only(self, db_session: AsyncSession,
                                                 cfg: AppConfig) -> None:
        ids = await seed(db_session, cfg)      # 不创建文件

        detail = await update_material(db_session, cfg, uuid.UUID(ids["美食/火锅.jpg"]),
                                       tags=["火锅", "美食", "火锅"])

        assert detail is not None and detail["tags"] == ["火锅", "美食"]
        assert not os.path.exists(str(Path(cfg.materials_dir()) / "美食" / "火锅.jpg.txt"))

    async def test_unknown_id_returns_none(self, db_session: AsyncSession,
                                          cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        assert await update_material(db_session, cfg, uuid.uuid4(), title="x") is None

    async def test_clear_tags(self, db_session: AsyncSession, cfg: AppConfig) -> None:
        media = touch("运动/挥拍.mp4", cfg)
        ids = await seed(db_session, cfg)

        detail = await update_material(db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]), tags=[])

        assert detail is not None and detail["tags"] == []
        assert parse_sidecar(media + ".txt")["tags"] == []

    async def test_patch_endpoint(self, client: TestClient, db_session: AsyncSession,
                                  cfg: AppConfig) -> None:
        touch("运动/挥拍.mp4", cfg)
        ids = await seed(db_session, cfg)

        ok = client.patch(f"/api/materials/{ids['运动/挥拍.mp4']}",
                          json={"title": "接口改的", "tags": ["球场"]})
        assert ok.status_code == 200, ok.text
        assert ok.json()["title"] == "接口改的"
        assert ok.json()["tags"] == ["球场"]

        missing = client.patch(f"/api/materials/{uuid.uuid4()}", json={"title": "x"})
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"


async def add_run_match(session: AsyncSession, material_id: str, cfg: AppConfig) -> None:
    """给素材挂一条历史候选（`run_matches`），用来验证移入回收站时的级联清理。"""
    hotspot = Hotspot(raw_text=f"热点-{material_id[:8]}", clue={})
    session.add(hotspot)
    await session.flush()
    run = Run(job_id=f"job-{uuid.uuid4()}", status="succeeded")
    session.add(run)
    await session.flush()
    run_hotspot = RunHotspot(run_id=run.id, hotspot_id=hotspot.id, position=1)
    session.add(run_hotspot)
    await session.flush()
    session.add(RunMatch(run_hotspot_id=run_hotspot.id,
                         material_id=uuid.UUID(material_id), rank=1,
                         score=Decimal("0.8000"), recall_sources=["literal"], reasons=["命中"]))
    await session.commit()


class TestTrash:
    async def test_moves_file_and_deletes_row_with_cascade(self, db_session: AsyncSession,
                                                          cfg: AppConfig) -> None:
        media = touch("运动/挥拍.mp4", cfg)
        Path(media + ".txt").write_text("标题: 手工\n", encoding="utf-8")
        ids = await seed(db_session, cfg)
        await add_run_match(db_session, ids["运动/挥拍.mp4"], cfg)

        result = await trash_material(db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]))

        assert result == {"material_id": ids["运动/挥拍.mp4"], "path": "运动/挥拍.mp4",
                          "trashed_path": "运动/挥拍.mp4", "file_missing": False}
        assert not os.path.exists(media)
        trashed = Path(trash_root(cfg)) / "运动" / "挥拍.mp4"
        assert trashed.is_file()
        assert (Path(str(trashed) + ".txt")).read_text(encoding="utf-8") == "标题: 手工\n"
        assert await load_material(db_session, cfg,
                                   uuid.UUID(ids["运动/挥拍.mp4"])) is None
        matches = (await db_session.execute(select(RunMatch))).scalars().all()
        assert matches == []                      # 级联清理：历史候选不再回来

    async def test_missing_file_still_deletes_row(self, db_session: AsyncSession,
                                                 cfg: AppConfig) -> None:
        ids = await seed(db_session, cfg)          # 不创建文件
        result = await trash_material(db_session, cfg, uuid.UUID(ids["美食/火锅.jpg"]))
        assert result is not None and result["file_missing"] is True
        assert await load_material(db_session, cfg,
                                   uuid.UUID(ids["美食/火锅.jpg"])) is None

    async def test_name_collision_gets_suffix(self, db_session: AsyncSession,
                                             cfg: AppConfig) -> None:
        touch("运动/挥拍.mp4", cfg)
        put_trash_file(cfg, "运动/挥拍.mp4")
        ids = await seed(db_session, cfg)

        result = await trash_material(db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]))

        assert result is not None and result["trashed_path"] == "运动/挥拍-2.mp4"

    async def test_unknown_id_returns_none(self, db_session: AsyncSession,
                                          cfg: AppConfig) -> None:
        await seed(db_session, cfg)
        assert await trash_material(db_session, cfg, uuid.uuid4()) is None


def put_trash_file(cfg: AppConfig, relative: str, data: bytes = b"old") -> str:
    path = Path(trash_root(cfg)) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


class TestRestoreAndPurge:
    async def test_restore_moves_back_and_enqueues(self, client: TestClient,
                                                   cfg: AppConfig) -> None:
        put_trash_file(cfg, "运动/挥拍.mp4")
        put_trash_file(cfg, "运动/挥拍.mp4.txt", "标题: 手工\n".encode())

        response = client.post("/api/materials/trash/restore", json={"path": "运动/挥拍.mp4"})

        assert response.status_code == 202, response.text
        assert response.json() == {"task_id": "task-1"}
        restored = Path(cfg.materials_dir()) / "运动" / "挥拍.mp4"
        assert restored.is_file()
        assert (restored.parent / "挥拍.mp4.txt").read_text(encoding="utf-8") == "标题: 手工\n"
        assert list_trash(cfg) == {"items": [], "total": 0}

    async def test_restore_then_index_creates_new_uuid(self, task_cfg: AppConfig,
                                                       db_session: AsyncSession,
                                                       cfg: AppConfig) -> None:
        """恢复 = 重新入库：索引任务跑完拿到的是**新 uuid**。"""
        touch("运动/挥拍.mp4", cfg)
        ids = await seed(db_session, cfg)
        await trash_material(db_session, cfg, uuid.UUID(ids["运动/挥拍.mp4"]))

        restore_from_trash(cfg, "运动/挥拍.mp4")
        report = await run_index(task_cfg, embedder=FakeEmbedder())

        assert report["added"] == 1 and report["scanned"] >= 1
        path = str(Path(cfg.materials_dir()) / "运动" / "挥拍.mp4")
        rows = (await db_session.execute(
            select(MaterialRow).where(MaterialRow.path == path))).scalars().all()
        assert len(rows) == 1
        assert str(rows[0].id) != ids["运动/挥拍.mp4"]

    async def test_purge_selected_and_all(self, cfg: AppConfig, client: TestClient) -> None:
        put_trash_file(cfg, "运动/挥拍.mp4")
        put_trash_file(cfg, "运动/挥拍.mp4.txt")
        put_trash_file(cfg, "美食/火锅.jpg")

        selected = purge_trash(cfg, paths=["运动/挥拍.mp4"])
        assert selected["deleted"] == ["运动/挥拍.mp4", "运动/挥拍.mp4.txt"]
        remaining = list_trash(cfg)
        assert remaining["total"] == 1

        response = client.post("/api/materials/trash/purge",
                               json={"all": True, "confirm": True})
        assert response.status_code == 200, response.text
        assert response.json()["count"] == 1
        assert list_trash(cfg)["total"] == 0


class TestIndexTask:
    async def test_run_index_syncs_new_file(self, task_cfg: AppConfig,
                                            db_session: AsyncSession,
                                            cfg: AppConfig) -> None:
        touch("运动/新片.mp4", cfg)
        Path(str(Path(cfg.materials_dir()) / "运动" / "新片.mp4") + ".txt").write_text(
            "标题: 新片\n标签: 羽毛球, 球场\n描述: 新上传的素材\n", encoding="utf-8")

        report = await run_index(task_cfg, embedder=FakeEmbedder())

        assert report["added"] == 1 and report["vision_used"] == 0
        assert report["embedded"] == 1
        row = (await db_session.execute(
            select(MaterialRow).where(MaterialRow.title == "新片"))).scalar_one()
        assert row.tags == ["羽毛球", "球场"] and row.source == "sidecar"
        assert "素材同步" in report["summary"]

    async def test_task_is_registered_under_the_contract_name(
            self, task_cfg: AppConfig) -> None:
        app = build_celery_app(task_cfg)
        task = register_index_task(app, task_cfg)
        assert task.name == INDEX_TASK_NAME
        assert app.tasks[INDEX_TASK_NAME].name == INDEX_TASK_NAME

    async def test_dispatcher_enqueues_into_real_redis(self, task_cfg: AppConfig) -> None:
        """真 Redis：设了结果后端之后投递仍然成功，任务的初始状态是 PENDING（没有 worker 消费）。"""
        dispatcher = CeleryDispatcher(task_cfg)

        task_id = await dispatcher.enqueue_index()

        assert len(task_id) == 36
        assert AsyncResult(task_id, app=dispatcher.app()).state == "PENDING"

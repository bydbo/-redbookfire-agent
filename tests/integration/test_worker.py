"""Celery worker 的集成用例（S3.4b）：真 Redis 投递 + 真 Postgres 写回 + 假模型。

覆盖：投递真的进 Redis 队列、任务消费后落库、前置的"索引新鲜度（同步 + 向量回填）"真的执行、
重复投递幂等、分析失败写 failed、broker 不可达时接口返回 503 且不留脏行。

任务体用 `task.apply()` 在**线程里**跑（`asyncio.to_thread`）：`execute` 内部要 `asyncio.run`，
而用例本身跑在 pytest-asyncio 的事件循环里，不能嵌套。
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import struct
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import CeleryDispatcher, get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import (
    EMBEDDING_DIM,
    Hotspot,
    Material,
    Run,
    RunHotspot,
    RunMatch,
)
from xhs_agent.tasks.analysis import register_analyze_task
from xhs_agent.tasks.celery_app import build_celery_app
from xhs_agent.tools import llm
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.llm import LLMResult, StructuredCaller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
RAW = "某明星打球场被拍，反差感拉满"
QUEUE = "celery"                     # Celery 默认队列名
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

CLUE_PAYLOAD = {
    "hotspot_raw": "模型改写版（应被输入覆盖）",
    "why_it_works": ["反差：明星身份 vs 业余球场"],
    "mechanisms": [{"name": "反差", "explain": "身份反差"}],
    "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9,
                  "evidence": "热点原文片段"}],
    "match_keywords": ["羽毛球"],
    "borrow_angles": ["同款球场热场"],
}
DRAFT_PAYLOAD = {"titles": [{"text": "球场热身也能出片", "style": "直给"}], "body": "正文……",
                 "tags": ["羽毛球"], "cover_text": "封面", "first_3s": "开头",
                 "shot_list": ["先拍球场"], "compliance_notes": ["别用明星肖像"]}


def _png(width: int = 1080, height: int = 1920) -> bytes:
    body = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    return body + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeEmbedder:
    """固定返回 E0：查询向量与素材向量一致，向量通道必然命中。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(vectors=[list(E0) for _ in texts], model=MODEL,
                               prompt_tokens=12, attempts=1)


class FakeProvider(llm.BaseProvider):
    """material_select 要与候选一一对应，所以从 prompt 里回读候选 uuid 再逐条解释。"""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(model="fake-1")
        self.tasks: list[str] = []
        self.fail = fail

    async def complete(self, call):
        self.tasks.append(call.task)
        if self.fail:
            return LLMResult(text="", provider=self.name, model=self.model, error="假上游失败")
        if call.task == "material_select":
            ids = list(dict.fromkeys(UUID_RE.findall(call.user)))
            payload = {"candidates": [
                {"material_id": material_id, "rank": index,
                 "reasons": [f"模型理由：命中主题「羽毛球」（{index}）"],
                 "usage": "模型用法：放开头 3 秒"}
                for index, material_id in enumerate(ids, start=1)
            ]}
        else:
            payload = {"hotspot_clue": CLUE_PAYLOAD, "copy_draft": DRAFT_PAYLOAD}[call.task]
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.0001)


def make_pack(tmp_path: Path) -> Path:
    """假素材包：1 张带旁车的图（标题含关键词，字面通道能命中）+ 1 张只有文件名的图。"""
    root = tmp_path / "materials"
    root.mkdir()
    (root / "球场-挥拍.png").write_bytes(_png())
    (root / "球场-挥拍.png.txt").write_text(
        "标题: 羽毛球球场热身\n标签: 羽毛球, 运动\n描述: 户外球场挥拍\n", encoding="utf-8")
    (root / "晨跑-公园.png").write_bytes(_png(720, 1280))
    return root


def write_config(tmp_path: Path, pack: Path, *, max_retries: int = 0) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{pack.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "index").as_posix()}"\n'
        "[embedding]\nenabled = true\n"
        f'model = "{MODEL}"\n'
        f"[queue]\nmax_retries = {max_retries}\n",
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_dsn: str,
        redis_url: str) -> AppConfig:
    """临时配置 + 容器 DSN：worker 自己建 engine，所以 DATABASE_URL 必须真的指到容器。"""
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    return write_config(tmp_path, make_pack(tmp_path))


@pytest_asyncio.fixture
async def session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as opened:
        yield opened


@pytest.fixture
def client(db_engine: AsyncEngine, cfg: AppConfig) -> Iterator[TestClient]:
    """真投递器（不覆盖 get_dispatcher）：接口会把消息发到容器的 Redis。"""
    # check_startup=False：集成用例只验接口与 worker 链路，不去打真实依赖的启动检查
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as opened:
            yield opened

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def submit(client: TestClient, hotspots: list[str]) -> dict:
    response = client.post("/api/analyze", json={"hotspots": hotspots})
    assert response.status_code == 202, response.text
    return response.json()


async def run_task(cfg: AppConfig, job_id: str, *, provider: FakeProvider | None = None,
                   embedder: FakeEmbedder | None = None):
    """在线程里跑任务体（`execute` 要 asyncio.run，不能在已有事件循环里嵌套）。"""
    app = build_celery_app(cfg)
    caller = StructuredCaller(provider=provider) if provider is not None else None
    task = register_analyze_task(app, cfg, caller=caller, embedder=embedder, vision=None)
    return await asyncio.to_thread(task.apply, args=[job_id])


async def counts(session: AsyncSession) -> tuple[int, int, int]:
    async def _count(model) -> int:
        return int((await session.execute(
            select(func.count()).select_from(model))).scalar_one())
    return await _count(Run), await _count(RunHotspot), await _count(RunMatch)


class TestQueueWiring:
    async def test_analyze_publishes_a_message(self, client: TestClient,
                                               redis_client) -> None:
        redis_client.delete(QUEUE)
        body = submit(client, [RAW])
        assert body["job_id"] and body["run_id"]
        assert redis_client.llen(QUEUE) == 1          # 消息真的进了 Redis 队列

    async def test_broker_down_returns_503_without_dirty_rows(
            self, client: TestClient, cfg: AppConfig, db_engine: AsyncEngine,
            monkeypatch: pytest.MonkeyPatch) -> None:
        dead = f"redis://127.0.0.1:{_free_port()}/0"
        dead_cfg = load_config(cfg.config_path)
        dead_cfg._env = EnvView({}, {"REDIS_URL": dead})
        client.app.dependency_overrides[get_dispatcher] = lambda: CeleryDispatcher(dead_cfg)

        response = client.post("/api/analyze", json={"hotspots": [RAW]})
        assert response.status_code == 503
        assert response.json()["code"] == "dependency_unavailable"
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            assert await counts(session) == (0, 0, 0)   # 投递失败整体回滚，不留脏 queued 行


class TestTaskBody:
    async def test_consumes_job_and_writes_back(self, client: TestClient, cfg: AppConfig,
                                                session: AsyncSession) -> None:
        body = submit(client, [RAW])
        provider = FakeProvider()

        result = await run_task(cfg, body["job_id"], provider=provider,
                                embedder=FakeEmbedder())

        assert result.state == "SUCCESS", result.traceback
        assert result.get() == "succeeded"
        assert provider.tasks == ["hotspot_clue", "material_select", "copy_draft"]

        run = await session.get(Run, uuid.UUID(body["run_id"]))
        assert run.status == "succeeded" and run.error is None
        assert run.llm_calls == 3 and run.prompt_versions["hotspot_clue"] == 1

        run_hotspot = (await session.execute(
            select(RunHotspot).where(RunHotspot.run_id == run.id))).scalar_one()
        assert run_hotspot.status == "succeeded"
        assert run_hotspot.coverage["ratio"] == pytest.approx(1.0)
        assert run_hotspot.draft["body"] == "正文……"
        assert (await session.get(Hotspot, run_hotspot.hotspot_id)).clue["hotspot_raw"] == RAW

        matches = list((await session.execute(
            select(RunMatch).where(RunMatch.run_hotspot_id == run_hotspot.id)
            .order_by(RunMatch.rank))).scalars().all())
        assert matches and all(row.reasons for row in matches)

    async def test_index_freshness_syncs_and_backfills(self, client: TestClient,
                                                       cfg: AppConfig,
                                                       session: AsyncSession) -> None:
        """进 LangGraph 之前必须先同步素材并回填向量——否则向量通道会静默缺数据。"""
        body = submit(client, [RAW])
        await run_task(cfg, body["job_id"], provider=FakeProvider(), embedder=FakeEmbedder())

        rows = list((await session.execute(select(Material))).scalars().all())
        assert len(rows) == 2                                   # 素材包里的两张图都入库了
        assert all(row.embedding is not None for row in rows)    # 且都补上了向量
        assert all(row.embedding_model == MODEL for row in rows)

    async def test_duplicate_delivery_is_skipped(self, client: TestClient, cfg: AppConfig,
                                                 session: AsyncSession) -> None:
        body = submit(client, [RAW])
        await run_task(cfg, body["job_id"], provider=FakeProvider(), embedder=FakeEmbedder())
        before = await counts(session)

        again_provider = FakeProvider()
        result = await run_task(cfg, body["job_id"], provider=again_provider,
                                embedder=FakeEmbedder())

        assert result.state == "SUCCESS"
        assert result.get() == "succeeded"
        assert again_provider.tasks == []      # 终态直接返回：一次模型调用都不再发
        assert await counts(session) == before

    async def test_failed_analysis_marks_run_failed(self, client: TestClient, cfg: AppConfig,
                                                    session: AsyncSession) -> None:
        body = submit(client, [RAW])
        result = await run_task(cfg, body["job_id"], provider=FakeProvider(fail=True),
                                embedder=FakeEmbedder())

        assert result.state == "SUCCESS"       # 任务本身跑完了
        assert result.get() == "failed"        # 但这一批分析是失败的
        run = await session.get(Run, uuid.UUID(body["run_id"]))
        assert run.status == "failed" and run.error
        run_hotspot = (await session.execute(
            select(RunHotspot).where(RunHotspot.run_id == run.id))).scalar_one()
        assert run_hotspot.status == "failed" and run_hotspot.error
        assert await counts(session) == (1, 1, 0)   # 失败热点不产候选

"""S4.3 的端到端链路用例（真 Postgres 容器 + 假投递器 + 假模型 + 内存导出器，不联网）。

验收口径：`POST /api/analyze` 的 ASGI span → `db.submit_analysis` span 属于同一条 trace；
投递时捕获的 W3C `traceparent` 能把 worker 侧的 span 接回**同一条 trace**。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.core.tracing import current_trace_headers
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM
from xhs_agent.tasks.analysis import run_job
from xhs_agent.tools import llm
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.llm import LLMResult, StructuredCaller

pytestmark = [pytest.mark.integration]

MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)

CLUE_PAYLOAD = {
    "hotspot_raw": "模型改写版（应被输入覆盖）",
    "why_it_works": ["反差：明星身份 vs 业余球场"],
    "mechanisms": [{"name": "反差", "explain": "身份反差"}],
    "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9,
                  "evidence": "热点原文片段"}],
    "match_keywords": ["羽毛球"],
    "borrow_angles": ["同款球场热场"],
}


class FakeEmbedder:
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(vectors=[list(E0) for _ in texts], model=MODEL,
                               prompt_tokens=12, attempts=1)


class FakeProvider(llm.BaseProvider):
    """库里没有素材 → 没有候选 → 只调一次拆解。"""

    name = "fake"

    def __init__(self) -> None:
        super().__init__(model="fake-1")

    async def complete(self, call):
        return LLMResult(text=json.dumps(CLUE_PAYLOAD, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.0001)


class CapturingDispatcher:
    """假投递器：记下投递时的 job_id 与当时的 W3C 传播头。"""

    def __init__(self) -> None:
        self.job_id = ""
        self.headers: dict[str, str] = {}

    async def enqueue(self, job_id: str) -> None:
        self.job_id = job_id
        self.headers = current_trace_headers()


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, postgres_dsn: str) -> AppConfig:
    """临时配置：指向容器 DSN，但**明确不给 Langfuse 三件套**——追踪由测试自己的内存 provider 提供。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[embedding]\nenabled = true\n"
        f'model = "{MODEL}"\n',
        encoding="utf-8",
    )
    loaded = load_config(str(path))
    loaded._env = EnvView({}, {"DATABASE_URL": postgres_dsn})
    return loaded


@pytest.fixture
def exporter(otel_exporter: InMemorySpanExporter) -> InMemorySpanExporter:
    otel_exporter.clear()
    return otel_exporter


def spans_named(exporter: InMemorySpanExporter, needle: str) -> list:
    return [span for span in exporter.get_finished_spans() if needle in span.name]


class TestOtelChain:
    def test_api_db_and_worker_share_one_trace(self, db_engine: AsyncEngine, cfg: AppConfig,
                                               exporter: InMemorySpanExporter) -> None:
        dispatcher = CapturingDispatcher()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        app = create_app(cfg, check_startup=False)
        app.dependency_overrides[get_config] = lambda: cfg
        app.dependency_overrides[get_dispatcher] = lambda: dispatcher

        async def _session() -> AsyncIterator[AsyncSession]:
            async with factory() as session:
                yield session

        app.dependency_overrides[get_session] = _session

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/api/analyze",
                                   json={"hotspots": ["某明星打羽毛球被拍"], "topk": 5})
            assert response.status_code == 202

        # 1) API 侧：ASGI 服务端 span + 数据库 span 属于同一条 trace
        api_span = spans_named(exporter, "POST /api/analyze")[0]
        db_span_ = spans_named(exporter, "db.submit_analysis")[0]
        assert api_span.context.trace_id == db_span_.context.trace_id
        assert db_span_.parent is not None
        assert db_span_.parent.span_id == api_span.context.span_id
        assert dict(db_span_.attributes or {})["db.operation"] == "insert"

        # 2) 投递时带上了同一个 trace 的 traceparent
        traceparent = dispatcher.headers["traceparent"]
        assert f"{api_span.context.trace_id:032x}" in traceparent

        # 3) worker 侧接住它：DB span 仍在同一条 trace 上
        import asyncio

        asyncio.run(run_job(dispatcher.job_id, cfg, caller=StructuredCaller(FakeProvider()),
                            embedder=FakeEmbedder(), vision=None, sync_index=False,
                            carrier=dispatcher.headers))

        worker_writes = spans_named(exporter, "db.run_recorder")
        assert worker_writes, "worker 没有产出 DB span"
        assert all(span.context.trace_id == api_span.context.trace_id for span in worker_writes)

    def test_without_traceparent_worker_starts_its_own_trace(self, db_engine: AsyncEngine,
                                                             cfg: AppConfig,
                                                             exporter: InMemorySpanExporter) -> None:
        """追踪关闭（没有 carrier）时行为与 S4.2 一致：worker 自己开一条 trace。"""
        dispatcher = CapturingDispatcher()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async def _seed() -> str:
            from xhs_agent.services.runs import submit_analysis

            async with factory() as session:
                submission = await submit_analysis(session, ["冷门热点"])
                return submission.job_id

        import asyncio

        job_id = asyncio.run(_seed())
        exporter.clear()
        asyncio.run(run_job(job_id, cfg, caller=StructuredCaller(FakeProvider()),
                            embedder=FakeEmbedder(), vision=None, sync_index=False,
                            carrier={}))

        worker_writes = spans_named(exporter, "db.run_recorder")
        assert worker_writes and worker_writes[0].parent is None
        assert dispatcher.headers == {}          # 没有 span 时也不会凭空造头

"""追踪关闭路径的集成用例（S4.2）：真 Postgres 容器 + 假模型，不联网。

`LANGFUSE_*` 三件套缺失时，整批照常跑完（`runs.status=succeeded`），
且**从不构造 Langfuse 客户端**——证明追踪确实是旁路组件。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.core import tracing
from xhs_agent.core.tracing import NullTracer, get_tracer
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM, Run
from xhs_agent.services.runs import planned_hotspots, submit_analysis
from xhs_agent.tools import llm
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.llm import LLMResult, StructuredCaller
from xhs_agent.workflows import run_analysis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
LANGFUSE_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST",
                "LANGFUSE_CAPTURE_CONTENT")

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
    """固定返回 E0；库是空的，所以向量通道命中 0 条（不触发 material_select）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(vectors=[list(E0) for _ in texts], model=MODEL,
                               prompt_tokens=12, attempts=1)


class FakeProvider(llm.BaseProvider):
    """只提供拆解结果：库里没有素材 → 没有候选 → 只调一次模型。"""

    name = "fake"

    def __init__(self) -> None:
        super().__init__(model="fake-1")
        self.tasks: list[str] = []

    async def complete(self, call):
        self.tasks.append(call.task)
        return LLMResult(text=json.dumps(CLUE_PAYLOAD, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.0001)


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture(autouse=True)
def clean_langfuse_env(monkeypatch: pytest.MonkeyPatch):
    """缺键路径必须可复现：清掉进程里可能存在的 Langfuse 变量并重置单例。"""
    for name in LANGFUSE_ENV:
        monkeypatch.delenv(name, raising=False)
    tracing.reset_tracer()
    yield
    tracing.reset_tracer()


def write_config(tmp_path: Path) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[embedding]\nenabled = true\n"
        f'model = "{MODEL}"\n',
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    cfg._env = EnvView({}, {})      # 明确不带 Langfuse 三件套
    return cfg


class TestTracingDisabled:
    async def test_batch_succeeds_without_building_a_client(self, db_session: AsyncSession,
                                                           tmp_path: Path,
                                                           monkeypatch) -> None:
        monkeypatch.setattr(tracing, "build_client",
                            lambda _cfg: pytest.fail("缺三件套时不该构造 Langfuse 客户端"))
        cfg = write_config(tmp_path)
        submission = await submit_analysis(db_session, ["某明星打羽毛球"])
        raws = [raw for _position, raw in await planned_hotspots(db_session, submission.run_id)]

        result = await run_analysis(raws, cfg=cfg, session=db_session, run_id=submission.run_id,
                                    caller=StructuredCaller(provider=FakeProvider()),
                                    embedder=FakeEmbedder())

        assert result.status == "succeeded"
        assert result.prompt_versions == {"hotspot_clue": 1}
        assert isinstance(get_tracer(cfg), NullTracer)
        run = (await db_session.execute(
            select(Run).where(Run.id == uuid.UUID(submission.run_id)))).scalar_one()
        assert run.status == "succeeded" and run.error is None
        assert run.llm_calls == 1 and run.cost_cny > 0

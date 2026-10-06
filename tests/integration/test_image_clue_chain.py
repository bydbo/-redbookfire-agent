"""S6.9 图片热点解析的端到端集成用例（真 Postgres 容器 + 假多模态 + 假文本 provider）。

链路：`parse_image_clue` 解析一张图 → 带 `clues` 提交 → worker 的任务体跑完 →
`GET /api/runs/{run_id}` 里的线索与提交值逐字段一致。

同时证明**拆解节点被短路**：预置线索让 `hotspot_clue` 一次都不调用（假文本 provider 的
`tasks` 为空），`runs.prompt_versions` 里也就没有它——用户为图片解析付过的钱不会重复付。

全程不联网：多模态走 `httpx.MockTransport`，文本模型与向量走假实现；库里没有素材，
所以没有候选，`copy_draft` 也不会被调用。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM
from xhs_agent.services.image_clue import parse_image_clue
from xhs_agent.services.runs import planned_hotspots
from xhs_agent.tools import llm
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.llm import LLMResult, StructuredCaller
from xhs_agent.workflows import run_analysis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# 最小 PNG 头（校验只看 magic bytes，内容不必是真图片）
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)

CLUE_PAYLOAD: dict[str, Any] = {
    "raw_text": "球场上的球拍特写，背景是虚化的球网",
    "clue": {
        "why_it_works": ["把日常运动器材拍出陌生感"],
        "mechanisms": [{"name": "反差", "explain": "器材特写与运动场景错位"}],
        "elements": [{"type": "visual", "value": "球拍特写", "weight": 0.9,
                      "confidence": 0.85, "evidence": "画面正中是球拍"}],
        "match_keywords": ["球拍", "球场"],
        "borrow_angles": ["用同款球场做热场"],
        "risk_notes": ["不要带品牌 logo"],
    },
}


class FakeDispatcher:
    """假投递器：只记账，不起 Celery。"""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


class FakeEmbedder:
    """固定返回 E0；库是空的，所以向量通道命中 0 条。"""

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(vectors=[list(E0) for _ in texts], model=MODEL,
                               prompt_tokens=12, attempts=1)


class FakeProvider(llm.BaseProvider):
    """记账用的文本 provider：预置线索下应当一次都不被调用。"""

    name = "fake"

    def __init__(self) -> None:
        super().__init__(model="fake-1")
        self.tasks: list[str] = []

    async def complete(self, call) -> LLMResult:
        self.tasks.append(call.task)
        return LLMResult(text=json.dumps(CLUE_PAYLOAD, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.0001)


def vision_transport(model_text: str) -> httpx.MockTransport:
    """假多模态端点：按 OpenAI 兼容形状回一段 JSON 文本。"""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        return httpx.Response(200, json={"choices": [{"message": {"content": model_text}}]})
    return httpx.MockTransport(handler)


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    """临时配置：`[vision]` 打开并指向测试用的密钥变量，路径全落 tmp。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[vision]\n"
        "enabled = true\n"
        'api_key_env = "MY_VISION_KEY"\n'
        'model = "qwen-vl-max"\n'
        "[embedding]\n"
        "enabled = true\n"
        f'model = "{MODEL}"\n'
        "[frontend]\n"
        "serve = false\n",
        encoding="utf-8")
    monkeypatch.setenv("MY_VISION_KEY", "sk-vision-test")
    return load_config(str(path))


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def client(db_engine: AsyncEngine, cfg: AppConfig, dispatcher: FakeDispatcher,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """真应用 + 容器里的会话：`check_startup=False`，启动前置检查另有集成用例。"""
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestImageClueChain:
    async def test_parse_submit_run_and_read_back(self, client: TestClient,
                                                 db_session: AsyncSession,
                                                 cfg: AppConfig,
                                                 dispatcher: FakeDispatcher,
                                                 provider: FakeProvider,
                                                 embedder: FakeEmbedder) -> None:
        # 1) 解析图片（假多模态，不落盘不入库）
        async with httpx.AsyncClient(transport=vision_transport(
                json.dumps(CLUE_PAYLOAD, ensure_ascii=False))) as http:
            parsed = await parse_image_clue(cfg, PNG, "image/png", http=http)
        assert parsed.prompt_versions == {"image_hotspot_clue": 1}
        clue = parsed.clue.to_dict()
        assert clue["hotspot_raw"] == CLUE_PAYLOAD["raw_text"]

        # 2) 用户确认后带 clues 提交（走真实接口 → 落库 → 记 job_id）
        response = client.post("/api/analyze",
                               json={"hotspots": [parsed.raw_text], "clues": [clue]})
        assert response.status_code == 202, response.text
        accepted = response.json()
        assert dispatcher.enqueued == [accepted["job_id"]]
        run_id = accepted["run_id"]

        # 3) 跑 worker 的任务体（同样是真库、真图、真写回，模型是假的）
        raws = [raw for _position, raw in await planned_hotspots(db_session, run_id)]
        assert raws == [parsed.raw_text]
        result = await run_analysis(raws, cfg=cfg, session=db_session, run_id=run_id,
                                    caller=StructuredCaller(provider=provider),
                                    embedder=embedder)

        assert result.status == "succeeded"
        assert provider.tasks == []                    # 拆解被短路：一次模型调用都没有
        assert "hotspot_clue" not in result.prompt_versions

        # 4) 结果页读到的线索与提交值逐字段一致
        detail = client.get(f"/api/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["status"] == "succeeded"
        assert "hotspot_clue" not in body["prompt_versions"]
        assert body["hotspots"][0]["hotspot_raw"] == parsed.raw_text
        assert body["hotspots"][0]["clue"] == clue

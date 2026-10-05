"""五个接口的集成用例（S3.3）：真容器 + TestClient，覆盖状态机与契约形状。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.api.routers import ops
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import DependencyUnavailableError
from xhs_agent.db import Base
from xhs_agent.db.models import Hotspot, Material, Run, RunHotspot, RunMatch

pytestmark = [pytest.mark.integration]


class FakeDispatcher:
    """假投递器：记账或按需失败（S3.4b 之前用它覆盖 202 与 503 两条分支）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.enqueued: list[str] = []
        self.fail = fail

    async def enqueue(self, job_id: str) -> None:
        if self.fail:
            raise DependencyUnavailableError("假队列故障", {"job_id": job_id})
        self.enqueued.append(job_id)


def write_config(tmp_path: Path) -> AppConfig:
    """临时配置：路径指向 tmp（不写仓库），其余走契约默认值。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


def build_client(db_engine: AsyncEngine, cfg: AppConfig, dispatcher,
                 monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """建应用并把三个依赖指向容器与假投递器（health 的 db 探针也要指过去）。"""
    # check_startup=False：接口用例只验接口与错误语义，不去打真实依赖的启动检查
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    monkeypatch.setattr(ops, "get_engine", lambda: db_engine)
    return TestClient(app, raise_server_exceptions=False)


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redis_url: str) -> AppConfig:
    """临时配置：先在进程环境放好 REDIS_URL 与文本密钥，再加载（EnvView 是加载时的快照）。"""
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    return write_config(tmp_path)


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def client(db_engine: AsyncEngine, cfg: AppConfig, dispatcher: FakeDispatcher,
           monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with build_client(db_engine, cfg, dispatcher, monkeypatch) as test_client:
        yield test_client


async def _count(db_engine: AsyncEngine, model) -> int:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _finish_run(db_engine: AsyncEngine, run_id: str) -> str:
    """模拟 worker 写回：线索 + 覆盖度 + 文稿 + 候选 + runs 统计（S3.4a 的活，这里只为验证接口）。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        material = Material(path="D:/materials/球场-挥拍.mp4", type="video", title="球场热身",
                            description="户外球场挥拍", tags=["羽毛球"], elements=[],
                            source="sidecar", width=1080, height=1920, size_bytes=2048)
        session.add(material)
        run_hotspot = (await session.execute(
            select(RunHotspot).where(RunHotspot.run_id == uuid.UUID(run_id))
            .order_by(RunHotspot.position))).scalars().first()
        assert run_hotspot is not None
        run_hotspot.status = "succeeded"
        run_hotspot.coverage = {
            "ratio": 1.0,
            "covered": [{"type": "topic", "value": "羽毛球", "weight": 0.9,
                         "confidence": 0.9, "evidence": "热点原文"}],
            "gaps": [],
        }
        run_hotspot.draft = {"titles": [{"text": "球场热身也能出片", "style": "直给"}],
                             "body": "正文……", "tags": ["#羽毛球"], "cover_text": "封面",
                             "first_3s": "开头给挥拍特写", "shot_list": ["先拍球场"],
                             "compliance_notes": ["别用明星肖像"]}
        hotspot = (await session.execute(
            select(Hotspot).where(Hotspot.id == run_hotspot.hotspot_id))).scalar_one()
        hotspot.clue = {"hotspot_raw": hotspot.raw_text,
                        "why_it_works": ["反差：身份与场景错位"],
                        "mechanisms": [{"name": "反差", "explain": "身份反差"}],
                        "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9,
                                      "confidence": 0.9, "evidence": "热点原文"}],
                        "match_keywords": ["羽毛球"], "audience": {},
                        "borrow_angles": ["同款球场热场"],
                        "risk_notes": ["别用明星肖像"]}
        await session.flush()
        session.add(RunMatch(run_hotspot_id=run_hotspot.id, material_id=material.id, rank=1,
                             score=Decimal("0.8123"), recall_sources=["literal", "vector"],
                             hits=[{"element_type": "topic", "clue_value": "羽毛球",
                                    "hit_value": "球场热身", "similarity": 0.9,
                                    "contribution": 0.8}],
                             missing=[], reasons=["命中主题「羽毛球」↔ 素材标签「羽毛球」"],
                             usage="放开头 3 秒"))
        run = (await session.execute(select(Run).where(Run.id == uuid.UUID(run_id)))).scalar_one()
        run.status = "succeeded"
        run.finished_at = datetime.now(UTC)
        run.llm_calls, run.prompt_tokens, run.completion_tokens = 3, 300, 150
        run.cost_cny = Decimal("0.0123")
        run.latency_ms = 2400
        run.prompt_versions = {"hotspot_clue": 1, "material_select": 1, "copy_draft": 1}
        await session.commit()
        return str(material.id)


def _submit(client: TestClient, hotspots: list[str], **extra) -> dict:
    payload = {"hotspots": hotspots, **extra}
    response = client.post("/api/analyze", json=payload, headers={"X-Request-ID": "req-1"})
    assert response.status_code == 202, response.text
    return response.json()


class TestAnalyze:
    def test_creates_rows_and_dispatches(self, client: TestClient,
                                         dispatcher: FakeDispatcher) -> None:
        body = _submit(client, ["热点A", "热点B"], topk=3)
        assert set(body) == {"job_id", "run_id"}
        assert uuid.UUID(body["run_id"])                  # run_id 是 uuid
        assert dispatcher.enqueued == [body["job_id"]]     # 投递的是 job_id

    @pytest.mark.asyncio
    async def test_persists_expected_fields(self, client: TestClient, db_engine: AsyncEngine,
                                            dispatcher: FakeDispatcher) -> None:
        body = _submit(client, ["热点A", "热点B"], topk=3)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            run = (await session.execute(select(Run))).scalar_one()
            assert (run.job_id, run.status, run.topk, run.request_id) == \
                (body["job_id"], "queued", 3, "req-1")
            positions = list((await session.execute(
                select(RunHotspot.position).order_by(RunHotspot.position))).scalars())
            assert positions == [1, 2]
            assert set((await session.execute(select(RunHotspot.status))).scalars()) == {"queued"}
            hotspots = (await session.execute(select(Hotspot.raw_text))).scalars().all()
            assert sorted(hotspots) == ["热点A", "热点B"]
            clues = (await session.execute(select(Hotspot.clue))).scalars().all()
            assert all(clue == {} for clue in clues)      # 线索由 worker 回填，提交时是空对象
        assert dispatcher.enqueued == [body["job_id"]]

    @pytest.mark.asyncio
    async def test_reuses_existing_hotspot_across_requests(self, client: TestClient,
                                                          db_engine: AsyncEngine) -> None:
        _submit(client, ["同一句热点"])
        _submit(client, ["同一句热点", "新热点"])
        assert await _count(db_engine, Hotspot) == 2     # 同名热点复用，不重复建行
        assert await _count(db_engine, Run) == 2

    def test_rejects_out_of_contract_bodies(self, client: TestClient) -> None:
        for payload in ({}, {"hotspots": []}, {"hotspots": ["  "]},
                        {"hotspots": ["x"] * 11}, {"hotspots": ["x" * 501]},
                        {"hotspots": ["x"], "topk": 0}, {"hotspots": ["x"], "topk": 21},
                        {"hotspots": ["x"], "extra": 1}):
            response = client.post("/api/analyze", json=payload)
            assert response.status_code == 422, payload
            assert response.json()["code"] == "validation_error"

    @pytest.mark.asyncio
    async def test_dispatch_failure_rolls_back(self, db_engine: AsyncEngine, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch,
                                              redis_url: str) -> None:
        monkeypatch.setenv("REDIS_URL", redis_url)
        cfg = write_config(tmp_path)
        with build_client(db_engine, cfg, FakeDispatcher(fail=True), monkeypatch) as client:
            response = client.post("/api/analyze", json={"hotspots": ["热点"]})
        assert response.status_code == 503
        assert response.json()["code"] == "dependency_unavailable"
        assert await _count(db_engine, Run) == 0         # 整体回滚，不留 queued 脏行
        assert await _count(db_engine, Hotspot) == 0
        assert await _count(db_engine, RunHotspot) == 0


class TestJobStatus:
    def test_queued_job_reports_zero_progress(self, client: TestClient) -> None:
        accepted = _submit(client, ["热点A", "热点B"])
        response = client.get(f"/api/jobs/{accepted['job_id']}")
        assert response.status_code == 200
        body = response.json()
        assert (body["job_id"], body["run_id"], body["status"], body["progress"]) == \
            (accepted["job_id"], accepted["run_id"], "queued", 0.0)

    def test_unknown_job_returns_404(self, client: TestClient) -> None:
        response = client.get("/api/jobs/nope")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


class TestRunResults:
    def test_unfinished_run_is_conflict(self, client: TestClient) -> None:
        accepted = _submit(client, ["热点"])
        assert client.get(f"/api/runs/{accepted['run_id']}").status_code == 409
        report = client.get(f"/api/runs/{accepted['run_id']}/report")
        assert report.status_code == 409
        assert report.json()["code"] == "conflict"

    @pytest.mark.asyncio
    async def test_run_detail_matches_contract(self, client: TestClient,
                                               db_engine: AsyncEngine) -> None:
        accepted = _submit(client, ["某明星打羽毛球"])
        material_id = await _finish_run(db_engine, accepted["run_id"])

        response = client.get(f"/api/runs/{accepted['run_id']}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"run_id", "status", "created_at", "finished_at", "totals",
                             "prompt_versions", "hotspots"}
        assert body["status"] == "succeeded"
        assert body["prompt_versions"] == {"hotspot_clue": 1, "material_select": 1,
                                          "copy_draft": 1}
        assert body["totals"] == {"llm_calls": 3, "prompt_tokens": 300,
                                  "completion_tokens": 150, "cost_cny": 0.0123,
                                  "latency_ms": 2400}
        hotspot = body["hotspots"][0]
        assert set(hotspot) == {"hotspot_id", "hotspot_raw", "clue", "coverage",
                                "candidates", "draft"}
        assert hotspot["hotspot_raw"] == "某明星打羽毛球"
        assert hotspot["clue"]["elements"][0]["value"] == "羽毛球"
        assert hotspot["coverage"]["ratio"] == 1.0
        assert hotspot["draft"]["body"] == "正文……"

        candidate = hotspot["candidates"][0]
        assert set(candidate) == {"rank", "material_id", "score", "recall_sources", "hits",
                                  "missing", "reasons", "usage", "material"}
        assert candidate["material_id"] == material_id
        assert candidate["recall_sources"] == ["literal", "vector"]
        assert candidate["reasons"] and candidate["usage"]
        assert candidate["material"]["title"] == "球场热身"
        assert candidate["material"]["tags"] == ["羽毛球"]

    @pytest.mark.asyncio
    async def test_report_renders_markdown_and_html(self, client: TestClient,
                                                    db_engine: AsyncEngine) -> None:
        accepted = _submit(client, ["某明星打羽毛球"])
        await _finish_run(db_engine, accepted["run_id"])

        markdown = client.get(f"/api/runs/{accepted['run_id']}/report?format=md")
        assert markdown.status_code == 200
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert "某明星打羽毛球" in markdown.text
        assert "球场热身" in markdown.text

        html = client.get(f"/api/runs/{accepted['run_id']}/report")
        assert html.status_code == 200
        assert html.headers["content-type"].startswith("text/html")
        assert "<html" in html.text

    def test_missing_or_malformed_run_id_returns_404(self, client: TestClient) -> None:
        for run_id in (str(uuid.uuid4()), "not-a-uuid"):
            assert client.get(f"/api/runs/{run_id}").status_code == 404
            assert client.get(f"/api/runs/{run_id}/report").status_code == 404


class TestRunList:
    """S5.6：`GET /api/runs` 的分页、排序与摘要形状。"""

    def test_empty_database_returns_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/runs")
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [], "total": 0}

    def test_new_run_is_queued_with_zero_totals(self, client: TestClient) -> None:
        accepted = _submit(client, ["热点A", "热点B"], topk=3)
        body = client.get("/api/runs").json()
        assert body["total"] == 1
        item = body["items"][0]
        assert set(item) == {"run_id", "status", "created_at", "finished_at", "hotspot_count",
                             "hotspot_preview", "topk", "totals", "error"}
        assert (item["run_id"], item["status"], item["topk"]) == \
            (accepted["run_id"], "queued", 3)
        assert item["finished_at"] is None and item["error"] is None
        assert item["hotspot_count"] == 2
        assert item["hotspot_preview"] == "热点A"          # position=1 的那条
        assert item["totals"] == {"llm_calls": 0, "prompt_tokens": 0,
                                  "completion_tokens": 0, "cost_cny": 0.0, "latency_ms": 0}

    @pytest.mark.asyncio
    async def test_orders_by_created_at_desc_and_paginates(self, client: TestClient,
                                                           db_engine: AsyncEngine) -> None:
        first = _submit(client, ["第一条"])["run_id"]
        second = _submit(client, ["第二条"])["run_id"]
        third = _submit(client, ["第三条"])["run_id"]

        # 三连提交的 created_at 可能落在同一微秒，这里显式错开时间戳再断言顺序
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        base = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
        async with factory() as session:
            for order, run_id in enumerate((first, second, third)):
                run = (await session.execute(
                    select(Run).where(Run.id == uuid.UUID(run_id)))).scalar_one()
                run.created_at = base.replace(minute=order)
            await session.commit()

        page = client.get("/api/runs").json()
        assert page["total"] == 3
        assert [item["run_id"] for item in page["items"]] == [third, second, first]
        assert all(item["status"] == "queued" for item in page["items"])

        middle = client.get("/api/runs?limit=1&offset=1").json()
        assert middle["total"] == 3                       # total 是全量，不随分页变
        assert [item["run_id"] for item in middle["items"]] == [second]

    def test_long_hotspot_preview_is_truncated(self, client: TestClient) -> None:
        long_text = "长" * 80
        _submit(client, [long_text])
        item = client.get("/api/runs").json()["items"][0]
        assert item["hotspot_preview"] == "长" * 60 + "…"

    def test_out_of_contract_paging_params_are_rejected(self, client: TestClient) -> None:
        for query in ("limit=0", "limit=101", "offset=-1", "limit=abc"):
            assert client.get(f"/api/runs?{query}").status_code == 422, query


class TestHealth:
    def test_all_dependencies_up(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert set(body["checks"]) == {"db", "redis", "llm"}
        assert all(item["status"] == "ok" for item in body["checks"].values())
        assert body["checks"]["db"]["latency_ms"] >= 0

    def test_redis_down_returns_down_overall(self, db_engine: AsyncEngine, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")   # 不可达端口
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        cfg = write_config(tmp_path)
        with build_client(db_engine, cfg, FakeDispatcher(), monkeypatch) as client:
            response = client.get("/api/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "down"
        assert body["checks"]["redis"]["status"] == "down"
        assert body["checks"]["db"]["status"] == "ok"

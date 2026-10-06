"""接口层契约单测（离线）：请求模型校验、健康探针、投递器与错误码（S3.3）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from kombu.exceptions import KombuError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from xhs_agent.api.deps import CeleryDispatcher
from xhs_agent.api.main import API_PREFIX, create_app
from xhs_agent.api.routers import ops
from xhs_agent.api.routers.analysis import AnalyzeRequest
from xhs_agent.config import AppConfig, ConfigError, EnvView, load_config
from xhs_agent.core.errors import (
    CODE_STATUS,
    CONFLICT,
    ERROR_CODES,
    UPSTREAM_ERROR,
    ConflictError,
    DependencyUnavailableError,
)
from xhs_agent.tools.embedding import EmbeddingError
from xhs_agent.tools.llm import LLMError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = PROJECT_ROOT / "docs" / "contracts" / "openapi.yaml"


def make_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return load_config(str(path))


class TestAnalyzeRequest:
    def test_accepts_the_minimal_body(self):
        payload = AnalyzeRequest.model_validate({"hotspots": ["某明星打羽毛球"]})
        assert payload.topk == 5           # 契约默认值
        assert payload.hotspots == ["某明星打羽毛球"]

    def test_strips_whitespace(self):
        payload = AnalyzeRequest.model_validate({"hotspots": ["  热点  "], "topk": 3})
        assert payload.hotspots == ["热点"]
        assert payload.topk == 3

    @pytest.mark.parametrize("body", [
        {},                                            # 缺 hotspots
        {"hotspots": []},                              # 空数组
        {"hotspots": [""]},                            # 空字符串
        {"hotspots": ["   "]},                         # 只有空白
        {"hotspots": ["x"] * 11},                      # 超过 10 条
        {"hotspots": ["x" * 501]},                     # 单条超过 500 字
        {"hotspots": ["x"], "topk": 0},                # topk 越界
        {"hotspots": ["x"], "topk": 21},
        {"hotspots": ["x"], "extra": 1},               # 未知字段（契约 additionalProperties: false）
    ])
    def test_rejects_out_of_contract_bodies(self, body):
        with pytest.raises(ValidationError):
            AnalyzeRequest.model_validate(body)


class TestContractAlignment:
    def test_implemented_paths_match_the_contract(self):
        """实现的路由集合必须与契约逐条一致（契约先行，偏差当场红）。"""
        text = CONTRACT.read_text(encoding="utf-8")
        start, end = text.find("paths:"), text.find("components:")
        contract = sorted(re.findall(r"^  (/api/\S+):", text[start:end], re.M))
        implemented = sorted(create_app().openapi()["paths"])
        assert implemented == contract

    def test_contract_error_codes_cover_our_table(self):
        text = CONTRACT.read_text(encoding="utf-8")
        block = text[text.find("## 错误码"):text.find("license:")]
        for code in ERROR_CODES:
            assert f"`{code}`" in block, f"错误码 {code} 没写进契约"
        assert CONFLICT in CODE_STATUS and CODE_STATUS[CONFLICT] == 409


class TestHealthProbes:
    @pytest.mark.asyncio
    async def test_llm_probe_only_checks_configuration(self, tmp_path):
        cfg = make_config(tmp_path)
        down = ops.check_llm(cfg)
        assert down["status"] == "down" and cfg.llm.api_key_env in down["detail"]

        cfg.llm._env = EnvView({}, {cfg.llm.api_key_env: "sk-test"})
        ok = ops.check_llm(cfg)
        assert ok["status"] == "ok" and cfg.llm.model in ok["detail"]

    @pytest.mark.asyncio
    async def test_redis_probe_reports_missing_url(self, tmp_path):
        result = await ops.check_redis(make_config(tmp_path))
        assert result["status"] == "down" and "REDIS_URL" in result["detail"]

    @pytest.mark.asyncio
    async def test_database_probe_reports_unreachable_database(self, monkeypatch):
        engine = create_async_engine("postgresql+asyncpg://x:x@127.0.0.1:1/x")
        monkeypatch.setattr(ops, "get_engine", lambda: engine)
        try:
            result = await ops.check_database()
        finally:
            await engine.dispose()
        assert result["status"] == "down"
        assert "latency_ms" in result


class TestCeleryDispatcher:
    """S3.4b：默认投递器投 Celery；broker 不可达时折成 503（不做进程内假执行）。"""

    class BrokenApp:
        """假 Celery 应用：send_task 直接抛 kombu 的连接类错误（用例不真连 broker）。"""

        def send_task(self, *_args, **_kwargs):
            raise KombuError("broker 连不上")

    @pytest.fixture
    def dispatcher(self, tmp_path) -> CeleryDispatcher:
        cfg = make_config(tmp_path, "[queue]\nmax_retries = 1\n")
        cfg._env = EnvView({}, {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        instance = CeleryDispatcher(cfg)
        instance._app = self.BrokenApp()      # 注入假 app：不真连 Redis
        return instance

    @pytest.mark.asyncio
    async def test_broker_failure_becomes_dependency_unavailable(self, dispatcher):
        with pytest.raises(DependencyUnavailableError) as info:
            await dispatcher.enqueue("job-1")
        assert info.value.code == "dependency_unavailable"          # → HTTP 503
        assert info.value.detail["job_id"] == "job-1"
        assert "KombuError" in info.value.detail["error"]

    def test_redis_url_is_required(self, tmp_path):
        """缺 REDIS_URL 是配置错误（由接口层折成 503），不是静默跳过。"""
        with pytest.raises(ConfigError):
            CeleryDispatcher(make_config(tmp_path)).app()

    @pytest.mark.asyncio
    async def test_enqueue_passes_trace_headers_to_celery(self, tmp_path, otel_exporter):
        """S4.3：有活动 span 时把 W3C traceparent 放进消息头，worker 才能接住同一条 trace。"""
        from opentelemetry import trace

        from xhs_agent.api.deps import CeleryDispatcher

        recorded: dict = {}

        class RecordingApp:
            def send_task(self, *args, **kwargs):
                recorded["args"] = args
                recorded["kwargs"] = kwargs

        cfg = make_config(tmp_path)
        cfg._env = EnvView({}, {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        dispatcher = CeleryDispatcher(cfg)
        dispatcher._app = RecordingApp()        # type: ignore[assignment]

        tracer = trace.get_tracer("xhs_agent.test")
        with tracer.start_as_current_span("POST /api/analyze"):
            await dispatcher.enqueue("job-1")

        headers = recorded["kwargs"]["headers"]
        assert headers["traceparent"].startswith("00-")
        assert recorded["kwargs"]["args"] == ["job-1"]

    @pytest.mark.asyncio
    async def test_enqueue_without_span_sends_no_trace_headers(self, tmp_path):
        from xhs_agent.api.deps import CeleryDispatcher

        recorded: dict = {}

        class RecordingApp:
            def send_task(self, *args, **kwargs):
                recorded["kwargs"] = kwargs

        cfg = make_config(tmp_path)
        cfg._env = EnvView({}, {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        dispatcher = CeleryDispatcher(cfg)
        dispatcher._app = RecordingApp()        # type: ignore[assignment]
        await dispatcher.enqueue("job-1")
        assert recorded["kwargs"]["headers"] == {}

    @pytest.mark.asyncio
    async def test_analyze_is_sent_with_ignore_result(self, tmp_path):
        """S6.3 / ADR 0014：分析任务的投递显式 `ignore_result=True`。

        `send_task` 只在 `ignore_result=False` 时才调 `backend.on_task_call`——少了这个标记，
        设了结果后端就会把「broker 不可达 → 快速 503」变成几秒后抛裸 RuntimeError。
        """
        from xhs_agent.api.deps import CeleryDispatcher

        recorded: dict = {}

        class RecordingApp:
            def send_task(self, *args, **kwargs):
                recorded["args"] = args
                recorded["kwargs"] = kwargs

        cfg = make_config(tmp_path)
        cfg._env = EnvView({}, {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        dispatcher = CeleryDispatcher(cfg)
        dispatcher._app = RecordingApp()        # type: ignore[assignment]

        await dispatcher.enqueue("job-1")

        assert recorded["kwargs"]["ignore_result"] is True

    @pytest.mark.asyncio
    async def test_enqueue_index_returns_task_id_and_keeps_result(self, tmp_path):
        """素材索引任务相反：结果**要**存（`GET /api/materials/tasks/{id}` 靠它报进度）。"""
        from xhs_agent.api.deps import CeleryDispatcher
        from xhs_agent.tasks.indexing import INDEX_TASK_NAME

        recorded: dict = {}

        class RecordingApp:
            def send_task(self, *args, **kwargs):
                recorded["args"] = args
                recorded["kwargs"] = kwargs

        cfg = make_config(tmp_path)
        cfg._env = EnvView({}, {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        dispatcher = CeleryDispatcher(cfg)
        dispatcher._app = RecordingApp()        # type: ignore[assignment]

        task_id = await dispatcher.enqueue_index()

        assert len(task_id) == 36 and task_id.count("-") == 4      # uuid4
        assert recorded["args"] == (INDEX_TASK_NAME,)
        assert recorded["kwargs"]["task_id"] == task_id
        assert recorded["kwargs"]["args"] == [[]]
        assert "ignore_result" not in recorded["kwargs"]           # 默认 False = 存结果

    @pytest.mark.asyncio
    async def test_enqueue_index_broker_failure_is_dependency_unavailable(self, tmp_path):
        from xhs_agent.api.deps import CeleryDispatcher

        cfg = make_config(tmp_path)
        cfg._env = EnvView({}, {"REDIS_URL": "redis://127.0.0.1:6379/0"})
        dispatcher = CeleryDispatcher(cfg)
        dispatcher._app = self.BrokenApp()      # type: ignore[assignment]

        with pytest.raises(DependencyUnavailableError) as info:
            await dispatcher.enqueue_index()

        assert info.value.code == "dependency_unavailable"
        assert "KombuError" in info.value.detail["error"]


class TestConflictError:
    def test_conflict_maps_to_409(self):
        error = ConflictError("运行尚未完成", {"run_id": "x"})
        assert error.code == CONFLICT
        assert error.status == 409
        assert error.detail == {"run_id": "x"}


class TestUpstreamErrorMapping:
    """S3.5：模型的 `LLMError` 与向量的 `EmbeddingError` 折成契约的 `upstream_error`(502)。

    现在没有 API 请求会同步调上游（分析在 worker 里跑），所以用探针路由把映射钉住。
    """

    @pytest.fixture
    def client(self) -> TestClient:
        router = APIRouter()

        @router.get("/probe/llm-error")
        async def llm_failure():
            raise LLMError("上游 503")

        @router.get("/probe/embedding-error")
        async def embedding_failure():
            raise EmbeddingError("向量维度不符")

        app = create_app()
        app.include_router(router, prefix=API_PREFIX)
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize("path", ["/api/probe/llm-error", "/api/probe/embedding-error"])
    def test_upstream_failures_map_to_502(self, client: TestClient, path: str):
        response = client.get(path)
        assert response.status_code == CODE_STATUS[UPSTREAM_ERROR] == 502
        body = response.json()
        assert set(body) == {"code", "message", "detail"}
        assert body["code"] == UPSTREAM_ERROR
        assert body["code"] in ERROR_CODES
        assert body["detail"]["request_id"]
        assert "Error" in body["detail"]["error"]

"""FastAPI 骨架单测：`/api` 前缀、错误响应契约、X-Request-ID（离线，内存 ASGI）。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from xhs_agent.api import deps
from xhs_agent.api.main import API_PREFIX, create_app
from xhs_agent.config import ConfigError, load_config
from xhs_agent.core.errors import (
    BAD_REQUEST,
    CODE_STATUS,
    DEFAULT_MESSAGE,
    DEPENDENCY_UNAVAILABLE,
    ERROR_CODES,
    INTERNAL_ERROR,
    NOT_FOUND,
    UPSTREAM_ERROR,
    VALIDATION_ERROR,
    ApiError,
    BadRequestError,
    DependencyUnavailableError,
    InternalError,
    NotFoundError,
    UpstreamError,
    code_for_status,
)
from xhs_agent.core.logging import configure_logging, current_request_id
from xhs_agent.probe import E_CONFIG_MISSING, PreflightFailed, PreflightReport, Problem

ACCESS_LOGGER = "xhs_agent.api.access"


def async_return(value):
    """把一个值包成 async 函数（替换 `run_startup_checks` 这类协程依赖用）。"""
    async def _call(*_args, **_kwargs):
        return value
    return _call


def probe_router() -> APIRouter:
    """测试专用探针：把需要验证的错误分支挂到 /api 下（不改产品代码）。"""
    router = APIRouter(tags=["测试探针"])

    @router.get("/probe/bad-request")
    async def bad_request() -> dict[str, Any]:
        raise BadRequestError("参数不合法", {"field": "hotspots"})

    @router.get("/probe/not-found")
    async def not_found() -> dict[str, Any]:
        raise NotFoundError("run 不存在", {"run_id": "abc"})

    @router.get("/probe/upstream")
    async def upstream() -> dict[str, Any]:
        raise UpstreamError("模型连续重试失败")

    @router.get("/probe/dependency")
    async def dependency() -> dict[str, Any]:
        raise DependencyUnavailableError("数据库连不上")

    @router.get("/probe/internal")
    async def internal() -> dict[str, Any]:
        raise InternalError()

    @router.get("/probe/boom")
    async def boom() -> dict[str, Any]:
        raise ZeroDivisionError("秘密串")

    @router.get("/probe/validate")
    async def validate(topk: int) -> dict[str, Any]:
        return {"topk": topk}

    return router


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.include_router(probe_router(), prefix=API_PREFIX)
    return TestClient(app, raise_server_exceptions=False)


def assert_error_envelope(response, *, status: int, code: str) -> dict[str, Any]:
    """错误响应的统一断言：状态 + code + 三键齐备 + code 落在契约枚举内。"""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"code", "message", "detail"}, body
    assert body["code"] == code
    assert body["code"] in ERROR_CODES
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["detail"], dict)
    assert response.headers.get("X-Request-ID")
    return body


class TestApiPrefix:
    def test_unregistered_path_returns_contract_404(self, client: TestClient) -> None:
        # 注意：/api/health 自 S3.3 起是真接口，这里换一个仍未注册的路径
        body = assert_error_envelope(client.get(f"{API_PREFIX}/nope"),
                                    status=404, code=NOT_FOUND)
        assert body["detail"]["original_status"] == 404

    def test_prefix_is_required(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 404
        assert client.get("/runs/x").status_code == 404
        assert client.get("/openapi.json").status_code == 404   # 文档也挂在 /api 下

    def test_probe_route_is_mounted_under_api(self, client: TestClient) -> None:
        body = assert_error_envelope(client.get(f"{API_PREFIX}/probe/not-found"),
                                    status=404, code=NOT_FOUND)
        assert body["message"] == "run 不存在"          # 产品自己的文案
        assert body["detail"]["run_id"] == "abc"        # 调用方传的 detail 原样带出


class TestErrorEnvelope:
    @pytest.mark.parametrize(("path", "status", "code"), [
        ("bad-request", 400, BAD_REQUEST),
        ("not-found", 404, NOT_FOUND),
        ("upstream", 502, UPSTREAM_ERROR),
        ("dependency", 503, DEPENDENCY_UNAVAILABLE),
        ("internal", 500, INTERNAL_ERROR),
    ])
    def test_each_error_branch_matches_contract(self, client: TestClient, path: str,
                                                status: int, code: str) -> None:
        body = assert_error_envelope(client.get(f"{API_PREFIX}/probe/{path}"),
                                    status=status, code=code)
        assert body["detail"]["request_id"]                       # 每条错误都带 request_id
        assert status == CODE_STATUS[code]                        # code 与状态严格配对

    def test_validation_error_carries_field_errors(self, client: TestClient) -> None:
        body = assert_error_envelope(client.get(f"{API_PREFIX}/probe/validate"),
                                    status=422, code=VALIDATION_ERROR)
        assert body["detail"]["errors"]                            # 字段级明细
        assert body["message"] == DEFAULT_MESSAGE[VALIDATION_ERROR]

    def test_framework_http_exception_is_normalized(self, client: TestClient) -> None:
        """405 这类非契约状态码按最近错误码归一化，原始状态留在 detail。"""
        body = assert_error_envelope(client.post(f"{API_PREFIX}/probe/not-found"),
                                    status=400, code=BAD_REQUEST)
        assert body["detail"]["original_status"] == 405

    def test_unhandled_exception_hides_stack_trace(self, client: TestClient) -> None:
        body = assert_error_envelope(client.get(f"{API_PREFIX}/probe/boom"),
                                    status=500, code=INTERNAL_ERROR)
        raw = str(body)
        assert "秘密串" not in raw and "Traceback" not in raw and "ZeroDivisionError" not in raw
        assert body["message"] == DEFAULT_MESSAGE[INTERNAL_ERROR]
        assert set(body["detail"]) == {"request_id"}


class TestRequestId:
    def test_echoes_incoming_header(self, client: TestClient) -> None:
        response = client.get(f"{API_PREFIX}/probe/not-found",
                              headers={"X-Request-ID": "abc-123"})
        assert response.headers["X-Request-ID"] == "abc-123"
        assert response.json()["detail"]["request_id"] == "abc-123"

    def test_generates_uuid_when_absent(self, client: TestClient) -> None:
        first = client.get(f"{API_PREFIX}/probe/not-found").headers["X-Request-ID"]
        second = client.get(f"{API_PREFIX}/probe/not-found").headers["X-Request-ID"]
        assert uuid.UUID(first) and uuid.UUID(second)     # 形状正确
        assert first != second                            # 两次请求不同

    def test_error_response_also_carries_request_id(self, client: TestClient) -> None:
        response = client.get(f"{API_PREFIX}/probe/boom",
                              headers={"X-Request-ID": "trace-500"})
        assert response.status_code == 500
        assert response.headers["X-Request-ID"] == "trace-500"

    def test_context_is_reset_after_request(self, client: TestClient) -> None:
        client.get(f"{API_PREFIX}/probe/not-found", headers={"X-Request-ID": "leak-check"})
        assert current_request_id() == ""                 # 不泄漏到请求之外


class TestAccessLog:
    def test_access_log_carries_same_request_id(self, client: TestClient, caplog) -> None:
        configure_logging("INFO")     # 幂等：把 request_id 过滤器装到 caplog 的 handler 上
        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            response = client.get(f"{API_PREFIX}/probe/not-found",
                                  headers={"X-Request-ID": "req-42"})
        assert response.headers["X-Request-ID"] == "req-42"
        records = [item for item in caplog.records if item.name == ACCESS_LOGGER]
        assert records, "没有记录访问日志"
        assert all(item.request_id == "req-42" for item in records)
        assert "404" in records[-1].getMessage()

    def test_unhandled_exception_is_logged_with_request_id(self, client: TestClient,
                                                          caplog) -> None:
        configure_logging("INFO")
        with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
            client.get(f"{API_PREFIX}/probe/boom", headers={"X-Request-ID": "req-boom"})
        errors = [item for item in caplog.records if item.levelno >= logging.ERROR]
        assert any(item.request_id == "req-boom" for item in errors)   # 堆栈进日志


class TestErrorModel:
    @pytest.mark.parametrize(("status", "code"), [
        (400, BAD_REQUEST), (404, NOT_FOUND), (405, BAD_REQUEST), (409, BAD_REQUEST),
        (422, VALIDATION_ERROR), (500, INTERNAL_ERROR), (502, UPSTREAM_ERROR),
        (503, DEPENDENCY_UNAVAILABLE), (418, BAD_REQUEST), (599, INTERNAL_ERROR),
    ])
    def test_status_maps_to_contract_code(self, status: int, code: str) -> None:
        assert code_for_status(status) == code

    @pytest.mark.parametrize(("error", "code"), [
        (BadRequestError(), BAD_REQUEST),
        (NotFoundError(), NOT_FOUND),
        (DependencyUnavailableError(), DEPENDENCY_UNAVAILABLE),
        (UpstreamError(), UPSTREAM_ERROR),
        (InternalError(), INTERNAL_ERROR),
    ])
    def test_error_defaults(self, error: ApiError, code: str) -> None:
        assert error.code == code
        assert error.status == CODE_STATUS[code]
        assert error.message == DEFAULT_MESSAGE[code]
        assert error.detail == {}

    def test_custom_message_and_detail(self) -> None:
        error = BadRequestError("topk 越界", {"topk": 99})
        assert (error.message, error.detail) == ("topk 越界", {"topk": 99})


class TestAppFactory:
    def test_create_app_does_not_load_config(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("XHS_CONFIG_PATH", str(tmp_path / "missing.toml"))
        app = create_app()                      # 显式路径缺失时会报错，这里不报 = 导入期没读配置
        assert app.title.startswith("小红书热点搭子")
        assert app.docs_url == f"{API_PREFIX}/docs"

    def test_get_config_loads_and_caches(self, monkeypatch, tmp_path) -> None:
        deps.get_config.cache_clear()
        path = tmp_path / "config.toml"
        path.write_text('log_level = "WARNING"\n', encoding="utf-8")
        monkeypatch.setenv("XHS_CONFIG_PATH", str(path))
        try:
            cfg = deps.get_config()
            assert cfg.log_level == "WARNING"
            assert logging.getLogger().level == logging.WARNING   # 顺带把日志级别配好
            assert deps.get_config() is cfg                        # 进程级缓存
        finally:
            deps.get_config.cache_clear()
            configure_logging("INFO")

    def test_get_config_raises_on_missing_explicit_path(self, monkeypatch, tmp_path) -> None:
        deps.get_config.cache_clear()
        monkeypatch.setenv("XHS_CONFIG_PATH", str(tmp_path / "nope.toml"))
        try:
            with pytest.raises(ConfigError):
                deps.get_config()
        finally:
            deps.get_config.cache_clear()


class TestStartupCheckWiring:
    """S3.8：启动前置检查接进 lifespan，`check_startup` 可显式关掉。"""

    @staticmethod
    def _config(tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("", encoding="utf-8")
        return load_config(str(path))

    def test_flag_defaults_to_true(self) -> None:
        assert create_app().state.check_startup is True

    def test_flag_can_be_disabled(self, tmp_path) -> None:
        app = create_app(self._config(tmp_path), check_startup=False)
        assert app.state.check_startup is False

    def test_lifespan_aborts_startup_when_checks_fail(self, tmp_path, monkeypatch,
                                                      capsys) -> None:
        from xhs_agent.api import main as api_main

        report = PreflightReport(problems=[
            Problem(E_CONFIG_MISSING, "DEEPSEEK_API_KEY", "缺密钥", "写进 config/.env", step=1)])
        monkeypatch.setattr(api_main, "run_startup_checks", async_return(report))
        app = create_app(self._config(tmp_path))

        with pytest.raises(PreflightFailed), TestClient(app, raise_server_exceptions=False):
            pass
        err = capsys.readouterr().err
        assert "E_CONFIG_MISSING" in err
        assert "启动前置检查未通过" in err

    def test_disabled_check_is_not_run(self, tmp_path, monkeypatch) -> None:
        from xhs_agent.api import main as api_main

        called: list[str] = []
        monkeypatch.setattr(api_main, "run_startup_checks",
                            lambda *_a, **_k: called.append("checked"))
        app = create_app(self._config(tmp_path), check_startup=False)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/api/openapi.json").status_code == 200
        assert called == []

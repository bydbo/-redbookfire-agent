"""接口层契约单测（离线）：请求模型校验、健康探针、投递器与错误码（S3.3）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from xhs_agent.api.deps import QueueNotConfigured
from xhs_agent.api.main import create_app
from xhs_agent.api.routers import ops
from xhs_agent.api.routers.analysis import AnalyzeRequest
from xhs_agent.config import AppConfig, EnvView, load_config
from xhs_agent.core.errors import (
    CODE_STATUS,
    CONFLICT,
    ERROR_CODES,
    ConflictError,
    DependencyUnavailableError,
)

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


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_queue_not_configured_fails_loudly(self):
        with pytest.raises(DependencyUnavailableError) as info:
            await QueueNotConfigured().enqueue("job-1")
        assert info.value.code == "dependency_unavailable"          # → HTTP 503
        assert info.value.detail["job_id"] == "job-1"
        assert "S3.4b" in info.value.message


class TestConflictError:
    def test_conflict_maps_to_409(self):
        error = ConflictError("运行尚未完成", {"run_id": "x"})
        assert error.code == CONFLICT
        assert error.status == 409
        assert error.detail == {"run_id": "x"}

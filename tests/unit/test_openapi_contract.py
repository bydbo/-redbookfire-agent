"""契约一致性校验的单测（S4.6）：按路径加载脚本模块，离线、不联网、不连库。

正向用例就是 CI 的那道门（`test` job 的 pytest 会跑它，本地 pre-commit 也会跑）；
反向用例证明校验器**真能抓到漂移**——否则"永远绿"的校验器没人看得出来。
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from xhs_agent.api.main import create_app
from xhs_agent.api.models import RunDetail

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_openapi.py"


@pytest.fixture(scope="module")
def checker():
    """按路径加载校验脚本（`if __name__ == "__main__"` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location("check_openapi_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def app_schema() -> dict[str, Any]:
    return create_app().openapi()


class TestContractMatches:
    def test_current_app_covers_the_contract(self, checker) -> None:
        """核心断言：FastAPI 导出的 OpenAPI 必须覆盖 docs/contracts/openapi.yaml。"""
        assert checker.check() == []

    def test_script_main_returns_zero(self, checker) -> None:
        assert checker.main([]) == 0


class TestDetectsDrift:
    """反向自测：把契约或实现改坏，校验器必须报出来。"""

    def test_reports_missing_property_in_implementation(self, checker) -> None:
        impl = copy.deepcopy(app_schema())
        impl["components"]["schemas"]["RunDetail"]["properties"].pop("prompt_versions")
        diffs = checker.compare(impl, checker.load_contract())
        assert any("prompt_versions" in item for item in diffs), diffs

    def test_reports_enum_drift(self, checker) -> None:
        impl = copy.deepcopy(app_schema())
        impl["components"]["schemas"]["Element"]["properties"]["type"]["enum"] = ["topic"]
        diffs = checker.compare(impl, checker.load_contract())
        assert any("enum" in item for item in diffs), diffs

    def test_reports_status_code_the_contract_does_not_declare(self, checker) -> None:
        contract = copy.deepcopy(checker.load_contract())
        contract["paths"]["/api/health"]["get"]["responses"]["500"] = {"description": "假状态码"}
        diffs = checker.compare(app_schema(), contract)
        assert any("500" in item for item in diffs), diffs

    def test_reports_undocumented_extra_status_code(self, checker) -> None:
        """实现自己多加一个契约没有的状态码（且不在白名单）也要失败。"""
        impl = copy.deepcopy(app_schema())
        impl["paths"]["/api/jobs/{job_id}"]["get"]["responses"]["418"] = {"description": "彩蛋"}
        diffs = checker.compare(impl, checker.load_contract())
        assert any("418" in item for item in diffs), diffs

    def test_reports_missing_component_schema(self, checker) -> None:
        impl = copy.deepcopy(app_schema())
        impl["components"]["schemas"].pop("Health")
        diffs = checker.compare(impl, checker.load_contract())
        assert any("Health" in item for item in diffs), diffs

    def test_reports_missing_parameter(self, checker) -> None:
        impl = copy.deepcopy(app_schema())
        impl["paths"]["/api/analyze"]["post"]["parameters"] = []
        diffs = checker.compare(impl, checker.load_contract())
        assert any("X-Request-ID" in item for item in diffs), diffs


class TestResponseModels:
    def test_models_are_permissive_lower_bounds(self) -> None:
        """`extra="allow"`：契约是下界，响应体里多出来的字段不会被 pydantic 丢掉。"""
        payload = {
            "run_id": "0f0e4e2e-0000-0000-0000-000000000000",
            "status": "succeeded",
            "created_at": "2026-10-04T00:00:00+00:00",
            "totals": {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                       "cost_cny": 0.0, "latency_ms": 0},
            "prompt_versions": {},
            "hotspots": [],
            "extra_field": "keep-me",
        }
        dumped = RunDetail.model_validate(payload).model_dump()
        assert dumped["extra_field"] == "keep-me"
        assert set(dumped) >= set(payload)

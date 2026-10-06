"""`POST /api/analyze` 的 `clues` 参数（S6.7）单测：长度校验、透传形状与向后兼容。

落库与覆盖语义由集成用例 `tests/integration/test_clues_channel.py` 用真容器验证；
这里 `submit_analysis` 被替换成记录参数的空实现，只验接口层的校验与透传。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xhs_agent.api import deps
from xhs_agent.api.main import create_app
from xhs_agent.api.routers import analysis as analysis_router
from xhs_agent.config import AppConfig, load_config

RUN_ID = "0f0e4e2e-0000-0000-0000-000000000000"
CLUE: dict[str, Any] = {
    "why_it_works": ["自律夜跑与路边宵夜并置，形成反差"],
    "mechanisms": [{"name": "反差", "explain": "自律与放纵同框"}],
    "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9, "confidence": 0.9,
                  "evidence": "背对镜头慢跑"}],
    "match_keywords": ["夜跑", "夜景"],
    "borrow_angles": ["用同款夜景跑道做热场"],
}


def build_config(tmp_path) -> AppConfig:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text("[frontend]\nserve = false\n", encoding="utf-8")
    return load_config(str(toml_path))


@pytest.fixture
def captured(monkeypatch) -> dict[str, Any]:
    """替换掉 `submit_analysis`：记录参数并回一个假 Submission。"""
    seen: dict[str, Any] = {}

    async def fake_submit(session: Any, hotspots: Any, **kwargs: Any) -> SimpleNamespace:
        seen["hotspots"] = list(hotspots)
        seen.update(kwargs)
        return SimpleNamespace(job_id="job-1", run_id=RUN_ID)

    monkeypatch.setattr(analysis_router, "submit_analysis", fake_submit)
    return seen


@pytest.fixture
def client(tmp_path, captured) -> TestClient:
    async def _noop_enqueue(_job_id: str) -> None:
        return None

    app = create_app(build_config(tmp_path), check_startup=False)
    app.dependency_overrides[deps.get_config] = lambda: build_config(tmp_path)
    app.dependency_overrides[deps.get_session] = lambda: None
    app.dependency_overrides[deps.get_dispatcher] = lambda: SimpleNamespace(
        enqueue=_noop_enqueue)
    return TestClient(app, raise_server_exceptions=False)


class TestCluesRequest:
    def test_clues_are_forwarded_as_plain_dicts(self, client, captured):
        response = client.post("/api/analyze", json={"hotspots": ["某热点"], "clues": [CLUE]})

        assert response.status_code == 202, response.text
        assert captured["hotspots"] == ["某热点"]
        # 透传给服务层的是 JSON 可入库的 dict（pydantic 模型已经 dump 过）
        assert captured["clues"] == [CLUE]

    def test_null_clue_means_normal_extraction(self, client, captured):
        response = client.post("/api/analyze",
                               json={"hotspots": ["热点一", "热点二"], "clues": [None, CLUE]})

        assert response.status_code == 202
        assert captured["clues"] == [None, CLUE]

    def test_without_clues_behaviour_is_unchanged(self, client, captured):
        response = client.post("/api/analyze", json={"hotspots": ["某热点"]})

        assert response.status_code == 202
        assert captured["clues"] is None          # 不传 = 与以前完全一致

    def test_length_mismatch_is_bad_request(self, client, captured):
        response = client.post("/api/analyze",
                               json={"hotspots": ["热点一", "热点二"], "clues": [CLUE]})

        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"
        assert captured == {}                     # 没有落库、也没有投递

    def test_clue_must_match_the_contract_shape(self, client, captured):
        response = client.post("/api/analyze",
                               json={"hotspots": ["某热点"], "clues": [{"why_it_works": ["x"]}]})

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
        assert captured == {}

    def test_unknown_field_is_rejected(self, client):
        response = client.post("/api/analyze",
                               json={"hotspots": ["某热点"], "unexpected": 1})
        assert response.status_code == 422

"""素材库编辑的离线单测（S6.2）：标签规则、旁车渲染回读与请求体校验口径。

真库 + 真文件（写盘、`source` 变化）留在 `tests/integration/test_materials_api.py`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_agent.api.deps import get_config, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.services.materials import (
    MAX_TAGS,
    clean_tags,
    render_sidecar,
    single_line,
    tag_problem,
)
from xhs_agent.tools.materials import parse_sidecar


def write_config(tmp_path: Path) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    cfg = write_config(tmp_path)
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def _session() -> AsyncIterator[object]:
        yield object()          # 用例都在用到会话之前就该失败

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestTagRules:
    def test_cleans_and_dedupes_keeping_order(self) -> None:
        assert clean_tags([" 羽毛球 ", "#球场", "羽毛球", "", "  ", "球场"]) == ["羽毛球", "球场"]

    def test_folds_newlines_inside_a_tag(self) -> None:
        assert clean_tags(["羽毛\n球"]) == ["羽毛 球"]

    def test_accepts_up_to_limit(self) -> None:
        tags = [f"标签{i}" for i in range(MAX_TAGS)]
        assert tag_problem(tags) is None

    def test_rejects_too_many_tags(self) -> None:
        tags = [f"标签{i}" for i in range(MAX_TAGS + 1)]
        problem = tag_problem(tags)
        assert problem is not None and f"{MAX_TAGS} 个" in problem

    def test_rejects_overlong_tag(self) -> None:
        assert tag_problem(["x" * 24]) is None
        problem = tag_problem(["x" * 25])
        assert problem is not None and "24 字" in problem


class TestSidecarRoundTrip:
    def test_render_then_parse(self, tmp_path: Path) -> None:
        path = tmp_path / "素材.mp4.txt"
        path.write_text(render_sidecar(title="球场挥拍", tags=["羽毛球", "球场"],
                                       description="户外球场挥拍"),
                        encoding="utf-8")
        parsed = parse_sidecar(str(path))
        assert parsed["title"] == "球场挥拍"
        assert parsed["tags"] == ["羽毛球", "球场"]
        assert parsed["description"] == "户外球场挥拍"

    def test_colons_in_values_survive(self, tmp_path: Path) -> None:
        path = tmp_path / "素材.mp4.txt"
        path.write_text(render_sidecar(title="标题: 带冒号", tags=[],
                                       description="描述: 也带冒号"), encoding="utf-8")
        parsed = parse_sidecar(str(path))
        assert parsed["title"] == "标题: 带冒号"
        assert parsed["tags"] == []
        assert parsed["description"] == "描述: 也带冒号"

    def test_newlines_are_folded(self) -> None:
        assert single_line("第一行\n第二行") == "第一行 第二行"
        assert render_sidecar(title="a\nb", tags=[], description="c\n\nd").count("\n") == 3


class TestPatchValidation:
    def test_empty_body_is_bad_request(self, client: TestClient) -> None:
        response = client.patch("/api/materials/2f1b1c0e-0000-4000-8000-000000000000", json={})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"

    @pytest.mark.parametrize("body", [
        {"filename": "x"},                       # 白名单之外的字段
        {"title": 123},                          # 类型不对
        {"tags": "不是数组"},
    ])
    def test_out_of_contract_body_is_validation_error(self, client: TestClient,
                                                     body: dict) -> None:
        response = client.patch("/api/materials/2f1b1c0e-0000-4000-8000-000000000000", json=body)
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    def test_too_many_tags_is_bad_request(self, client: TestClient) -> None:
        body = {"tags": [f"标签{i}" for i in range(MAX_TAGS + 1)]}
        response = client.patch("/api/materials/2f1b1c0e-0000-4000-8000-000000000000", json=body)
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"

    def test_malformed_uuid_is_not_found(self, client: TestClient) -> None:
        response = client.patch("/api/materials/not-a-uuid", json={"title": "x"})
        assert response.status_code == 404

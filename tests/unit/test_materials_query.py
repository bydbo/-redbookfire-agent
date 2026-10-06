"""素材库接口的离线单测（S6.1）：参数校验、非法 id 与 `dir` 计算的纯函数口径。

真库查询口径（分页 / 筛选 / 主题计数）留在 `tests/integration/test_materials_api.py`；
这里只在**不碰数据库**的分支上验：这些用例的 session 依赖一旦被真正使用就会炸。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_agent.api.deps import get_config, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.services.materials import relative_dir


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
        yield object()          # 用例都在用到会话之前就该失败，这里只保证依赖能构造

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestRelativeDir:
    def test_root_file_has_empty_dir(self, tmp_path: Path) -> None:
        root = str(tmp_path / "materials")
        assert relative_dir(str(tmp_path / "materials" / "a.mp4"), root) == ""

    def test_nested_dirs_use_forward_slashes(self, tmp_path: Path) -> None:
        root = str(tmp_path / "materials")
        path = str(tmp_path / "materials" / "运动" / "夜里" / "a.mp4")
        assert relative_dir(path, root) == "运动/夜里"


class TestQueryValidation:
    @pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
    def test_paging_out_of_range_is_validation_error(self, client: TestClient,
                                                     query: str) -> None:
        response = client.get(f"/api/materials?{query}")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    @pytest.mark.parametrize("query", ["type=audio", "source=unknown"])
    def test_unknown_enum_is_validation_error(self, client: TestClient, query: str) -> None:
        response = client.get(f"/api/materials?{query}")
        assert response.status_code == 422

    @pytest.mark.parametrize("material_id", ["not-a-uuid", "123", "运动"])
    def test_malformed_uuid_is_not_found(self, client: TestClient,
                                         material_id: str) -> None:
        response = client.get(f"/api/materials/{material_id}")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


class TestContractShape:
    def test_paths_are_registered(self, client: TestClient) -> None:
        """挂载点存在即可：完整形状由 `scripts/check_openapi.py` 与集成用例守。"""
        schema = client.get("/api/openapi.json").json()
        assert "/api/materials" in schema["paths"]
        assert "/api/materials/{material_id}" in schema["paths"]
        assert schema["paths"]["/api/materials"]["get"]["tags"] == ["素材"]

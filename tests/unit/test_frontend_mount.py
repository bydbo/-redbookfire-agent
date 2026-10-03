"""前端静态资源挂载的单测（S3.7）：离线，临时 dist + `TestClient`。

用例都用 `with TestClient(...)` 进入，以便真的触发 lifespan（挂载发生在启动钩子里）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_agent.api import main as api_main
from xhs_agent.api.frontend import (
    dist_dir_if_available,
    mount_frontend,
    require_dist_dir,
)
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, ConfigError, load_config

INDEX = "<!doctype html><div id=\"app\"></div>"
ASSET = "console.log('hi')"


def write_config(tmp_path: Path, *, serve: bool, dist_dir: str) -> AppConfig:
    path = tmp_path / "config.toml"
    # TOML 字符串里反斜杠是转义符，Windows 路径必须用正斜杠（仓库内其它用例同口径）
    path.write_text(
        "[frontend]\n"
        f"serve = {'true' if serve else 'false'}\n"
        f'dist_dir = "{Path(dist_dir).as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


def make_dist(root: Path) -> Path:
    """最小可用的 Vite 风格产物：index.html + assets/app.js。"""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "assets" / "app.js").write_text(ASSET, encoding="utf-8")
    return dist


@pytest.fixture
def serving_client(tmp_path: Path) -> Iterator[TestClient]:
    dist = make_dist(tmp_path)
    cfg = write_config(tmp_path, serve=True, dist_dir=str(dist))
    with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
        yield client


class TestMountedApp:
    def test_serves_index_at_root(self, serving_client: TestClient) -> None:
        response = serving_client.get("/")
        assert response.status_code == 200
        assert INDEX in response.text

    def test_serves_static_asset(self, serving_client: TestClient) -> None:
        response = serving_client.get("/assets/app.js")
        assert response.status_code == 200
        assert ASSET in response.text

    def test_extensionless_miss_falls_back_to_index(self, serving_client: TestClient) -> None:
        """前端 history 路由：/runs/abc 这类地址刷新后要拿到 index.html。"""
        response = serving_client.get("/runs/abc")
        assert response.status_code == 200
        assert INDEX in response.text

    def test_missing_asset_stays_404(self, serving_client: TestClient) -> None:
        """带扩展名的未命中不能变成 200 的 HTML，否则缺失资源极难排查。"""
        response = serving_client.get("/assets/missing.js")
        assert response.status_code == 404
        assert "text/html" not in response.headers.get("content-type", "")

    def test_api_prefix_is_not_swallowed(self, serving_client: TestClient) -> None:
        """SPA 不能吞掉 API 的 404：契约要求 code=not_found 的 JSON。"""
        response = serving_client.get("/api/nope")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_api_docs_and_schema_stay_available(self, serving_client: TestClient) -> None:
        assert serving_client.get("/api/openapi.json").status_code == 200
        assert serving_client.get("/api/docs").status_code == 200


class TestServeDisabled:
    def test_only_api_is_served(self, tmp_path: Path) -> None:
        dist = make_dist(tmp_path)
        cfg = write_config(tmp_path, serve=False, dist_dir=str(dist))
        with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
            root = client.get("/")
            assert root.status_code == 404
            assert root.json()["code"] == "not_found"
            assert client.get("/api/openapi.json").status_code == 200


class TestAvailability:
    def test_missing_dist_is_not_mounted(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path, serve=True, dist_dir=str(tmp_path / "nope"))
        app = create_app()
        assert mount_frontend(app, cfg) is False
        assert getattr(app.state, "frontend_mounted", False) is False

    def test_empty_dist_is_not_mounted(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        cfg = write_config(tmp_path, serve=True, dist_dir=str(empty))
        assert dist_dir_if_available(cfg) is None

    def test_mount_is_idempotent(self, tmp_path: Path) -> None:
        dist = make_dist(tmp_path)
        cfg = write_config(tmp_path, serve=True, dist_dir=str(dist))
        app = create_app(cfg)
        routes = len(app.routes)
        assert mount_frontend(app, cfg) is True      # 已挂过 → 直接返回 True
        assert len(app.routes) == routes             # 不会挂第二次


class TestRequireDistDir:
    def test_usable_dist_returns_absolute_path(self, tmp_path: Path) -> None:
        dist = make_dist(tmp_path)
        cfg = write_config(tmp_path, serve=True, dist_dir=str(dist))
        assert require_dist_dir(cfg) == str(dist)

    def test_serve_disabled_returns_none(self, tmp_path: Path) -> None:
        dist = make_dist(tmp_path)
        cfg = write_config(tmp_path, serve=False, dist_dir=str(dist))
        assert require_dist_dir(cfg) is None

    def test_missing_dist_raises_with_actionable_fix(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path, serve=True, dist_dir=str(tmp_path / "nope"))
        with pytest.raises(ConfigError) as info:
            require_dist_dir(cfg)
        assert "前端构建产物" in info.value.message
        assert "pnpm build" in info.value.fix        # 修复提示要能照做

    def test_empty_dist_raises_too(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        cfg = write_config(tmp_path, serve=True, dist_dir=str(empty))
        with pytest.raises(ConfigError):
            require_dist_dir(cfg)


class TestLifespanPath:
    def test_uvicorn_style_app_mounts_at_startup(self, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
        """`create_app()` 不传配置（uvicorn 的路径）：导入期仍不读配置，启动时才读并挂载。"""
        dist = make_dist(tmp_path)
        cfg = write_config(tmp_path, serve=True, dist_dir=str(dist))
        monkeypatch.setattr(api_main, "get_config", lambda: cfg)

        app = api_main.create_app()
        assert not getattr(app.state, "frontend_mounted", False)   # 建应用时没读配置
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/").status_code == 200
        assert app.state.frontend_mounted is True

    def test_config_error_at_startup_is_not_swallowed(self, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> AppConfig:
            raise ConfigError("配置坏了", "按提示修")

        monkeypatch.setattr(api_main, "get_config", boom)
        app = api_main.create_app()
        with pytest.raises(ConfigError), TestClient(app, raise_server_exceptions=False):
            pass

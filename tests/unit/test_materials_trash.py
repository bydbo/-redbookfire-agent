"""回收站的离线单测（S6.3）：路径安全、真删/恢复的参数口径与接口形状。

「删库 + 移文件」的完整链路（含 `run_matches` 级联）留在
`tests/integration/test_materials_api.py`；这里只用一个临时素材目录，不碰数据库。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import BadRequestError
from xhs_agent.services.materials import resolve_under, trash_root, unique_path


class FakeIndexDispatcher:
    """假投递器：只记 `enqueue_index` 的调用（素材接口不该投分析任务）。"""

    def __init__(self) -> None:
        self.index_calls: list[list[str] | None] = []

    async def enqueue(self, job_id: str) -> None:
        raise AssertionError("素材接口不该投分析任务")

    async def enqueue_index(self, *, force_paths: list[str] | None = None) -> str:
        self.index_calls.append(force_paths)
        return "task-1"


def write_config(tmp_path: Path) -> AppConfig:
    materials = tmp_path / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{materials.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return write_config(tmp_path)


@pytest.fixture
def dispatcher() -> FakeIndexDispatcher:
    return FakeIndexDispatcher()


@pytest.fixture
def client(cfg: AppConfig, dispatcher: FakeIndexDispatcher) -> Iterator[TestClient]:
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher

    async def _session() -> AsyncIterator[object]:
        yield object()          # 这些用例都在用到会话之前就该失败

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def put_trash(cfg: AppConfig, relative: str, data: bytes = b"fake") -> str:
    path = Path(trash_root(cfg)) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


class TestPathSafety:
    @pytest.mark.parametrize("bad", ["", "   ", ".", "..", "/etc/passwd", "../外面.mp4",
                                     "运动/../../外面.mp4"])
    def test_rejects_escaping_paths(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(BadRequestError):
            resolve_under(str(tmp_path / "_trash"), bad)

    def test_accepts_nested_relative_path(self, tmp_path: Path) -> None:
        root = str(tmp_path / "_trash")
        assert resolve_under(root, "运动/夜里/夜跑.mp4") == os.path.join(
            os.path.realpath(root), "运动", "夜里", "夜跑.mp4")

    def test_unique_path_adds_index(self, tmp_path: Path) -> None:
        target = tmp_path / "a.mp4"
        assert unique_path(str(target)) == str(target)
        target.write_bytes(b"x")
        assert unique_path(str(target)) == str(tmp_path / "a-2.mp4")
        (tmp_path / "a-2.mp4").write_bytes(b"x")
        assert unique_path(str(target)) == str(tmp_path / "a-3.mp4")


class TestTrashListing:
    def test_empty_trash(self, client: TestClient) -> None:
        response = client.get("/api/materials/trash")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0}

    def test_lists_media_only(self, client: TestClient, cfg: AppConfig) -> None:
        put_trash(cfg, "运动/挥拍.mp4", b"video-bytes")
        put_trash(cfg, "运动/挥拍.mp4.txt", "标题: x\n".encode())     # 旁车不单独列出
        put_trash(cfg, "美食/火锅.jpg")

        body = client.get("/api/materials/trash").json()

        assert body["total"] == 2
        assert [item["path"] for item in body["items"]] == ["美食/火锅.jpg", "运动/挥拍.mp4"]
        video = next(item for item in body["items"] if item["path"].endswith(".mp4"))
        assert video["type"] == "video"
        assert video["name"] == "挥拍.mp4"
        assert video["size_bytes"] == len(b"video-bytes")
        assert "T" in video["mtime"]                          # ISO 8601


class TestRestore:
    def test_moves_back_and_enqueues_index(self, client: TestClient, cfg: AppConfig,
                                           dispatcher: FakeIndexDispatcher) -> None:
        put_trash(cfg, "运动/挥拍.mp4")
        put_trash(cfg, "运动/挥拍.mp4.txt", "标题: 手工\n".encode())

        response = client.post("/api/materials/trash/restore", json={"path": "运动/挥拍.mp4"})

        assert response.status_code == 202, response.text
        assert response.json() == {"task_id": "task-1"}
        assert dispatcher.index_calls == [None]
        restored = Path(cfg.materials_dir()) / "运动" / "挥拍.mp4"
        assert restored.is_file()
        assert (restored.parent / "挥拍.mp4.txt").read_text(encoding="utf-8") == "标题: 手工\n"
        assert not (Path(trash_root(cfg)) / "运动" / "挥拍.mp4").exists()

    def test_missing_file_is_not_found(self, client: TestClient) -> None:
        response = client.post("/api/materials/trash/restore", json={"path": "没有这个.mp4"})
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_escaping_path_is_bad_request(self, client: TestClient) -> None:
        response = client.post("/api/materials/trash/restore", json={"path": "../外面.mp4"})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"

    def test_target_exists_is_conflict(self, client: TestClient, cfg: AppConfig) -> None:
        put_trash(cfg, "运动/挥拍.mp4")
        target = Path(cfg.materials_dir()) / "运动" / "挥拍.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"already-here")

        response = client.post("/api/materials/trash/restore", json={"path": "运动/挥拍.mp4"})

        assert response.status_code == 409
        assert response.json()["code"] == "conflict"

    def test_body_must_carry_path(self, client: TestClient) -> None:
        assert client.post("/api/materials/trash/restore", json={}).status_code == 422


class TestPurge:
    def test_requires_confirmation(self, client: TestClient, cfg: AppConfig) -> None:
        put_trash(cfg, "运动/挥拍.mp4")
        response = client.post("/api/materials/trash/purge", json={"all": True})
        assert response.status_code == 400
        assert "confirm" in response.json()["message"]

    @pytest.mark.parametrize("body", [{}, {"confirm": True},
                                      {"paths": ["运动/挥拍.mp4"], "all": True, "confirm": True}])
    def test_paths_and_all_are_exclusive_and_required(self, client: TestClient,
                                                      body: dict) -> None:
        assert client.post("/api/materials/trash/purge", json=body).status_code == 400

    def test_purge_selected_deletes_companions(self, client: TestClient,
                                              cfg: AppConfig) -> None:
        put_trash(cfg, "运动/挥拍.mp4")
        put_trash(cfg, "运动/挥拍.mp4.txt")
        put_trash(cfg, "美食/火锅.jpg")

        response = client.post("/api/materials/trash/purge",
                               json={"paths": ["运动/挥拍.mp4"], "confirm": True})

        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": ["运动/挥拍.mp4", "运动/挥拍.mp4.txt"], "count": 2}
        assert (Path(cfg.materials_dir()) / "_trash" / "美食" / "火锅.jpg").is_file()
        assert not (Path(cfg.materials_dir()) / "_trash" / "运动").exists()   # 空目录也清掉

    def test_purge_all_clears_everything(self, client: TestClient, cfg: AppConfig) -> None:
        put_trash(cfg, "运动/挥拍.mp4")
        put_trash(cfg, "美食/火锅.jpg")

        response = client.post("/api/materials/trash/purge",
                               json={"all": True, "confirm": True})

        assert response.status_code == 200, response.text
        assert response.json()["count"] == 2
        assert client.get("/api/materials/trash").json() == {"items": [], "total": 0}

    def test_unknown_path_is_not_found(self, client: TestClient, cfg: AppConfig) -> None:
        put_trash(cfg, "运动/挥拍.mp4")
        response = client.post("/api/materials/trash/purge",
                               json={"paths": ["运动/挥拍.mp4", "没有.mp4"], "confirm": True})
        assert response.status_code == 404
        # 先整体校验再删：一条不存在就一条都不删
        assert (Path(cfg.materials_dir()) / "_trash" / "运动" / "挥拍.mp4").is_file()


class TestTrashByMaterial:
    def test_malformed_uuid_is_not_found(self, client: TestClient) -> None:
        response = client.post("/api/materials/not-a-uuid/trash")
        assert response.status_code == 404

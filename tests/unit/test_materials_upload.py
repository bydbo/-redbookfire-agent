"""扫描 / 上传 / 任务查询的离线单测（S6.4）。

上传的成功路径不碰数据库（落盘 + 投递都是本进程的事），所以整个上传契约都能在这里钉住；
真 Redis 上的任务状态读写与「上传 → 索引 → 列表可见」留在
`tests/integration/test_materials_index.py`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.core.errors import BadRequestError, PayloadTooLargeError
from xhs_agent.services.materials import safe_filename, save_upload, upload_dir
from xhs_agent.tasks.indexing import task_view


class FakeUpload:
    """`fastapi.UploadFile` 的窄替身：只实现服务层用到的两个成员。"""

    def __init__(self, filename: str | None, data: bytes, chunk: int = 4096) -> None:
        self.filename = filename
        self._data = data
        self._chunk = chunk
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        step = self._chunk if size is None or size < 0 else min(size, self._chunk)
        chunk = self._data[self._offset:self._offset + step]
        self._offset += len(chunk)
        return chunk


class FakeDispatcher:
    """假投递器：记账 + 可注入的任务状态。"""

    def __init__(self) -> None:
        self.index_calls = 0
        self.state: tuple[str, Any] = ("PENDING", None)

    async def enqueue(self, job_id: str) -> None:
        raise AssertionError("素材接口不该投分析任务")

    async def enqueue_index(self, *, force_paths: list[str] | None = None) -> str:
        self.index_calls += 1
        return f"task-{self.index_calls}"

    async def task_state(self, task_id: str) -> tuple[str, Any]:
        assert task_id
        return self.state


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    materials = tmp_path / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{materials.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[frontend]\nserve = false\n" + body,
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return write_config(tmp_path)


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def client(cfg: AppConfig, dispatcher: FakeDispatcher) -> Iterator[TestClient]:
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher

    async def _session() -> AsyncIterator[object]:
        yield object()          # 上传 / 任务查询都不碰数据库

    app.dependency_overrides[get_session] = _session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class TestSafeFilename:
    def test_takes_basename_from_both_separators(self) -> None:
        assert safe_filename("C:\\Users\\x\\运动\\挥拍.mp4") == "挥拍.mp4"
        assert safe_filename("/tmp/aa/夜跑.mp4") == "夜跑.mp4"

    def test_replaces_windows_illegal_chars(self) -> None:
        assert safe_filename('a<b>c:d"e|f?g*h.mp4') == "a_b_c_d_e_f_g_h.mp4"

    @pytest.mark.parametrize("bad", [None, "", "   ", ".", "..", "\x00.mp4"])
    def test_rejects_empty_and_dot_names(self, bad: str | None) -> None:
        with pytest.raises(BadRequestError):
            safe_filename(bad)


class TestSaveUpload:
    @pytest.mark.asyncio
    async def test_lands_in_year_month_dir(self, cfg: AppConfig) -> None:
        saved = await save_upload(cfg, FakeUpload("挥拍.mp4", b"video-bytes"))

        assert saved["path"].startswith("20") and saved["path"].endswith("/挥拍.mp4")
        assert saved["name"] == "挥拍.mp4"
        assert saved["size_bytes"] == len(b"video-bytes")
        assert (Path(cfg.materials_dir()) / saved["path"]).read_bytes() == b"video-bytes"
        assert upload_dir(cfg).startswith(cfg.materials_dir())

    @pytest.mark.asyncio
    async def test_same_name_gets_suffix(self, cfg: AppConfig) -> None:
        await save_upload(cfg, FakeUpload("挥拍.mp4", b"first"))
        second = await save_upload(cfg, FakeUpload("挥拍.mp4", b"second"))
        assert second["name"] == "挥拍-2.mp4"

    @pytest.mark.asyncio
    async def test_rejects_extension_outside_allowlist(self, cfg: AppConfig) -> None:
        with pytest.raises(BadRequestError) as info:
            await save_upload(cfg, FakeUpload("脚本.exe", b"x"))
        assert "exe" in info.value.message

    @pytest.mark.asyncio
    async def test_rejects_empty_file(self, cfg: AppConfig) -> None:
        with pytest.raises(BadRequestError):
            await save_upload(cfg, FakeUpload("空.mp4", b""))

    @pytest.mark.asyncio
    async def test_oversize_is_payload_too_large_and_cleans_temp(
            self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path, "[upload]\nmax_size_gb = 0.000001\n")   # ≈1 KiB
        with pytest.raises(PayloadTooLargeError) as info:
            await save_upload(cfg, FakeUpload("大.mp4", b"x" * 4096))

        assert info.value.status == 413
        assert list(Path(cfg.materials_dir()).rglob("*.part")) == []
        assert list(Path(cfg.materials_dir()).rglob("*.mp4")) == []

    @pytest.mark.asyncio
    async def test_extension_normalization(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path, '[upload]\nallowed_extensions = ["MP4", ".JPG"]\n')
        assert cfg.upload.allowed_extensions == [".mp4", ".jpg"]
        saved = await save_upload(cfg, FakeUpload("A.MP4", b"x"))
        assert saved["name"] == "A.MP4"


class TestUploadEndpoint:
    def test_uploads_and_enqueues(self, client: TestClient, cfg: AppConfig,
                                 dispatcher: FakeDispatcher) -> None:
        response = client.post("/api/materials/uploads",
                               files={"file": ("挥拍.mp4", b"video-bytes", "video/mp4")})

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["task_id"] == "task-1"
        assert body["name"] == "挥拍.mp4"
        assert (Path(cfg.materials_dir()) / body["path"]).is_file()
        assert dispatcher.index_calls == 1

    def test_rejects_bad_extension(self, client: TestClient,
                                   dispatcher: FakeDispatcher) -> None:
        response = client.post("/api/materials/uploads",
                               files={"file": ("恶意.exe", b"x", "application/octet-stream")})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"
        assert dispatcher.index_calls == 0

    def test_requires_file_field(self, client: TestClient) -> None:
        assert client.post("/api/materials/uploads").status_code == 422


class TestScanEndpoint:
    def test_scan_enqueues_index(self, client: TestClient,
                                 dispatcher: FakeDispatcher) -> None:
        response = client.post("/api/materials/scan")
        assert response.status_code == 202, response.text
        assert response.json() == {"task_id": "task-1"}
        assert dispatcher.index_calls == 1


class TestTaskView:
    def test_pending_is_bare(self) -> None:
        assert task_view("t", "PENDING", None) == {
            "task_id": "t", "state": "PENDING", "step": None, "summary": None, "result": None}

    def test_started_carries_step(self) -> None:
        view = task_view("t", "STARTED", {"step": "向量回填"})
        assert view["state"] == "STARTED" and view["step"] == "向量回填"

    def test_success_carries_summary_and_result(self) -> None:
        info = {"scanned": 3, "added": 1, "summary": "素材同步：扫描 3、新增 1"}
        view = task_view("t", "SUCCESS", info)
        assert view["summary"] == info["summary"]
        assert view["result"] == info
        assert view["step"] is None

    def test_failure_truncates_message(self) -> None:
        view = task_view("t", "FAILURE", RuntimeError("x" * 900))
        assert view["state"] == "FAILURE"
        assert view["summary"] == "x" * 500

    def test_unknown_state_falls_back_to_started(self) -> None:
        assert task_view("t", "WEIRD", None)["state"] == "STARTED"


class TestTaskEndpoint:
    def test_reads_state_from_dispatcher(self, client: TestClient,
                                         dispatcher: FakeDispatcher) -> None:
        dispatcher.state = ("SUCCESS", {"scanned": 2, "added": 2, "summary": "好了"})

        response = client.get("/api/materials/tasks/task-1")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "SUCCESS" and body["summary"] == "好了"
        assert body["result"]["scanned"] == 2

    def test_unknown_task_is_pending(self, client: TestClient) -> None:
        response = client.get("/api/materials/tasks/不存在的任务")
        assert response.status_code == 200
        assert response.json()["state"] == "PENDING"


class TestUploadConfig:
    def test_defaults(self, tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        assert cfg.upload.max_size_gb == 2
        assert ".mp4" in cfg.upload.allowed_extensions
        assert len(cfg.upload.allowed_extensions) == len(set(cfg.upload.allowed_extensions))

    def test_defaults_match_media_tool(self, tmp_path: Path) -> None:
        """默认清单与 `tools/media.py` 的 `MEDIA_EXT` 同源（两处漂了这里就红）。"""
        from xhs_agent.tools.media import IMAGE_EXT, VIDEO_EXT

        cfg = write_config(tmp_path)
        assert set(cfg.upload.allowed_extensions) == set(VIDEO_EXT) | set(IMAGE_EXT)

    @pytest.mark.parametrize("body", ["[upload]\nmax_size_gb = 0\n",
                                      "[upload]\nallowed_extensions = []\n"])
    def test_invalid_values_are_rejected(self, tmp_path: Path, body: str) -> None:
        with pytest.raises(ConfigError):
            write_config(tmp_path, body)

    def test_describe_exposes_upload_block(self, tmp_path: Path) -> None:
        described = write_config(tmp_path).describe()
        assert described["upload"]["max_size_gb"] == 2
        assert ".mp4" in described["upload"]["allowed_extensions"]


class TestDockerfileShipsFfmpeg:
    def test_runtime_installs_ffmpeg(self) -> None:
        """S6.4：容器要能抽帧 / 探测 / 出缩略图（素材库页的缩略图依赖它）。"""
        dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text(
            encoding="utf-8")
        assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile
        assert "rm -rf /var/lib/apt/lists/*" in dockerfile

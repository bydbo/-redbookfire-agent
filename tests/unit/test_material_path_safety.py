"""E6 文件边界：旁车 / 上传目录 / 回收站旁车的链接与越界必须明确失败。

Windows 上普通用户可能无法创建符号链接；创建失败时跳过该用例（不是代码通过）。
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import BadRequestError
from xhs_agent.services.materials import (
    companion_paths,
    save_upload,
    write_sidecar,
)


def make_config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "materials"
    root.mkdir()
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{root.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "runs" / "_index").as_posix()}"\n',
        encoding="utf-8")
    return load_config(str(path))


def _symlink(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不能创建符号链接，跳过链接边界用例")


def _dir_link(target: Path, link: Path) -> None:
    """目录链接：优先符号链接，Windows 无权限时退回 junction（不需要管理员）。"""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                      capture_output=True).returncode != 0:
        pytest.skip("当前环境不能创建目录链接，跳过链接边界用例")


class _Upload:
    filename = "clip.mp4"

    def __init__(self, data: bytes = b"video") -> None:
        self._data = data
        self._read = False

    async def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._data


class TestSidecarSafety:
    def test_sidecar_symlink_is_rejected(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        root = Path(cfg.materials_dir())
        media = root / "运动" / "a.mp4"
        media.parent.mkdir()
        media.write_bytes(b"x")
        outside = tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        _symlink(outside, Path(str(media) + ".txt"))
        with pytest.raises(ValueError):
            write_sidecar(str(media), title="t", tags=[], description="d",
                          root=str(root))
        assert outside.read_text(encoding="utf-8") == "outside"

    def test_companion_symlink_is_rejected(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        root = Path(cfg.materials_dir())
        media = root / "a.mp4"
        media.write_bytes(b"x")
        outside = tmp_path / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        _symlink(outside, Path(str(media) + ".md"))
        with pytest.raises(BadRequestError):
            companion_paths(str(media))


class TestUploadSafety:
    @pytest.mark.asyncio
    async def test_month_dir_symlink_is_rejected(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        root = Path(cfg.materials_dir())
        outside = tmp_path / "outside"
        outside.mkdir()
        # 预置一个链接指向素材根之外，上传必须拒绝而不是写穿
        _dir_link(outside, root / "2026-10")
        with pytest.raises((BadRequestError, OSError)):
            await save_upload(cfg, _Upload(), now=datetime(2026, 10, 19))
        assert not (outside / "clip.mp4").exists()

    @pytest.mark.asyncio
    async def test_regular_upload_lands_under_month_dir(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        saved = await save_upload(cfg, _Upload(b"video"), now=datetime(2026, 10, 19))
        assert saved["path"] == "2026-10/clip.mp4"
        assert (Path(cfg.materials_dir()) / saved["path"]).read_bytes() == b"video"

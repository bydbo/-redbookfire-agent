"""E6 文件边界：旁车 / 上传目录 / 回收站旁车的链接与越界必须明确失败。

Windows 上普通用户可能无法创建符号链接；创建失败时跳过该用例（不是代码通过）。
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import BadRequestError, ConflictError
from xhs_agent.services.materials import (
    _choose_group_target,
    companion_paths,
    list_trash,
    purge_trash,
    restore_from_trash,
    save_upload,
    trash_root,
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


class TestTrashBoundary:
    """R01：`_trash` 自身或子层指向素材根之外时，所有入口拒绝且零改动。"""

    def _setup(self, tmp_path: Path) -> tuple[AppConfig, Path]:
        cfg = make_config(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "sentinel.txt").write_text("sentinel", encoding="utf-8")
        (outside / "外部素材.mp4").write_bytes(b"outside")
        _dir_link(outside, Path(trash_root(cfg)))
        return cfg, outside

    def test_list_trash_rejects_linked_root(self, tmp_path: Path) -> None:
        cfg, outside = self._setup(tmp_path)
        with pytest.raises(BadRequestError):
            list_trash(cfg)
        assert (outside / "sentinel.txt").read_text(encoding="utf-8") == "sentinel"
        assert (outside / "外部素材.mp4").read_bytes() == b"outside"

    def test_purge_trash_rejects_linked_root(self, tmp_path: Path) -> None:
        cfg, outside = self._setup(tmp_path)
        with pytest.raises(BadRequestError):
            purge_trash(cfg, all_files=True)
        assert (outside / "sentinel.txt").exists()
        assert (outside / "外部素材.mp4").exists()

    def test_restore_rejects_linked_root(self, tmp_path: Path) -> None:
        cfg, outside = self._setup(tmp_path)
        with pytest.raises(BadRequestError):
            restore_from_trash(cfg, "外部素材.mp4")
        assert (outside / "外部素材.mp4").exists()


class TestFileGroup:
    def test_group_target_avoids_companion_conflict(self, tmp_path: Path) -> None:
        base = tmp_path / "a.mp4"
        (tmp_path / "a.mp4.txt").write_text("existing", encoding="utf-8")
        assert _choose_group_target(str(base), ["", ".txt"]) == str(tmp_path / "a-2.mp4")

    def test_restore_media_conflict_keeps_both_sides(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        root = Path(cfg.materials_dir())
        _put_trash(cfg, "运动/a.mp4", b"trashed")
        _put_trash(cfg, "运动/a.mp4.txt", b"trashed-sidecar")
        target = root / "运动"
        target.mkdir(parents=True)
        (target / "a.mp4").write_bytes(b"existing")
        with pytest.raises(ConflictError):
            restore_from_trash(cfg, "运动/a.mp4")
        assert (target / "a.mp4").read_bytes() == b"existing"
        assert (Path(trash_root(cfg)) / "运动" / "a.mp4").read_bytes() == b"trashed"
        assert (Path(trash_root(cfg)) / "运动" / "a.mp4.txt").read_bytes() == b"trashed-sidecar"

    def test_restore_companion_only_conflict_keeps_both_sides(self, tmp_path: Path) -> None:
        cfg = make_config(tmp_path)
        root = Path(cfg.materials_dir())
        _put_trash(cfg, "运动/a.mp4", b"trashed")
        _put_trash(cfg, "运动/a.mp4.txt", b"trashed-sidecar")
        target = root / "运动"
        target.mkdir(parents=True)
        (target / "a.mp4.txt").write_bytes(b"existing-sidecar")
        with pytest.raises(ConflictError):
            restore_from_trash(cfg, "运动/a.mp4")
        assert (target / "a.mp4.txt").read_bytes() == b"existing-sidecar"
        assert not (target / "a.mp4").exists()
        assert (Path(trash_root(cfg)) / "运动" / "a.mp4").exists()

    def test_move_failure_compensates(self, tmp_path: Path, monkeypatch) -> None:
        cfg = make_config(tmp_path)
        _put_trash(cfg, "运动/a.mp4", b"trashed")
        _put_trash(cfg, "运动/a.mp4.txt", b"trashed-sidecar")
        calls = {"n": 0}
        real_replace = os.replace

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("模拟第二个文件移动失败")
            real_replace(source, target)

        monkeypatch.setattr(os, "replace", flaky)
        with pytest.raises(ConflictError) as excinfo:
            restore_from_trash(cfg, "运动/a.mp4")
        assert excinfo.value.detail["reason"] == "file_group_move_failed"
        assert (Path(trash_root(cfg)) / "运动" / "a.mp4").read_bytes() == b"trashed"
        assert (Path(trash_root(cfg)) / "运动" / "a.mp4.txt").read_bytes() == b"trashed-sidecar"
        assert not (Path(cfg.materials_dir()) / "运动" / "a.mp4").exists()

    def test_compensation_failure_requires_manual_review(self, tmp_path: Path,
                                                        monkeypatch) -> None:
        cfg = make_config(tmp_path)
        _put_trash(cfg, "运动/a.mp4", b"trashed")
        _put_trash(cfg, "运动/a.mp4.txt", b"trashed-sidecar")
        calls = {"n": 0}
        real_replace = os.replace

        def flaky(source, target):
            calls["n"] += 1
            # 第 2 次移动失败；第 3 次是补偿（移回），也失败
            if calls["n"] >= 2:
                raise OSError("模拟移动与补偿都失败")
            real_replace(source, target)

        monkeypatch.setattr(os, "replace", flaky)
        with pytest.raises(ConflictError) as excinfo:
            restore_from_trash(cfg, "运动/a.mp4")
        assert excinfo.value.detail["manual_review_required"] is True
        log = Path(str(excinfo.value.detail["log"]))
        assert log.is_file()
        assert "manual_review_required" in log.read_text(encoding="utf-8")


def _put_trash(cfg: AppConfig, relative: str, data: bytes) -> None:
    target = Path(trash_root(cfg)) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


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

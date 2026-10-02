"""素材索引同步服务的离线单测：`plan_sync` 是纯函数，不连库、不联网、不依赖 ffmpeg。"""

from __future__ import annotations

import os

import pytest

from xhs_agent.config import ConfigError, load_config
from xhs_agent.services import materials as service
from xhs_agent.tools import materials as tool


def _row(path: str, fingerprint: str):
    """最小行替身：`plan_sync` 只需要 `path` 与 `fingerprint` 两个属性。"""
    return type("Row", (), {"path": path, "fingerprint": fingerprint})()


class TestPlanSync:
    def test_classifies_added_updated_unchanged_deleted(self, tmp_path):
        root = tmp_path / "materials"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"aaa")
        (root / "b.mp4").write_bytes(b"bbb")
        (root / "c.mp4").write_bytes(b"ccc")
        rows = [
            _row(str(root / "a.mp4"), tool.fingerprint(str(root / "a.mp4"))),
            _row(str(root / "c.mp4"), "stale-fingerprint"),
            _row(str(root / "gone.mp4"), "whatever"),
        ]
        disk = [str(root / "a.mp4"), str(root / "b.mp4"), str(root / "c.mp4")]
        plan = service.plan_sync(disk, rows, str(root))
        assert [os.path.basename(p) for p in plan.added] == ["b.mp4"]
        assert [os.path.basename(p) for p in plan.updated] == ["c.mp4"]
        assert [os.path.basename(p) for p in plan.unchanged] == ["a.mp4"]
        assert [os.path.basename(p) for p in plan.deleted] == ["gone.mp4"]
        assert plan.counts() == (1, 1, 1, 1)

    def test_empty_fingerprint_counts_as_updated(self, tmp_path):
        root = tmp_path / "m"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"aaa")
        plan = service.plan_sync([str(root / "a.mp4")],
                                 [_row(str(root / "a.mp4"), "")], str(root))
        assert plan.updated == [os.path.abspath(str(root / "a.mp4"))]
        assert plan.unchanged == []

    def test_rows_outside_scan_dir_are_never_deleted(self, tmp_path):
        root = tmp_path / "m"
        root.mkdir()
        (root / "a.mp4").write_bytes(b"aaa")
        outside = tmp_path / "other" / "x.mp4"
        plan = service.plan_sync([str(root / "a.mp4")],
                                 [_row(str(outside), "fp")], str(root))
        assert plan.deleted == []
        assert plan.added == [os.path.abspath(str(root / "a.mp4"))]

    def test_empty_scan_does_not_delete_anything(self, tmp_path):
        root = tmp_path / "m"
        root.mkdir()
        plan = service.plan_sync([], [_row(str(root / "a.mp4"), "fp")], str(root))
        assert (plan.added, plan.updated, plan.unchanged, plan.deleted) == ([], [], [], [])

    def test_outputs_are_sorted(self, tmp_path):
        root = tmp_path / "m"
        root.mkdir()
        (root / "b.mp4").write_bytes(b"b")
        (root / "a.mp4").write_bytes(b"a")
        plan = service.plan_sync([str(root / "b.mp4"), str(root / "a.mp4")], [], str(root))
        assert plan.added == [os.path.abspath(str(root / "a.mp4")),
                              os.path.abspath(str(root / "b.mp4"))]


def _cfg(materials_dir: str, index_dir: str):
    return type("Cfg", (), {
        "materials_dir": lambda self: materials_dir,
        "index_dir": lambda self: index_dir,
    })()


class TestSyncMaterials:
    @pytest.mark.asyncio
    async def test_missing_materials_dir_raises(self, tmp_path):
        cfg = _cfg(str(tmp_path / "nope"), str(tmp_path / "idx"))
        with pytest.raises(ConfigError):
            # 目录检查在碰数据库之前，因此 session=None 不会被使用
            await service.sync_materials(None, cfg)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_missing_dir_error_carries_fix_hint(self, tmp_path):
        cfg = _cfg(str(tmp_path / "nope"), str(tmp_path / "idx"))
        with pytest.raises(ConfigError) as excinfo:
            await service.sync_materials(None, cfg)  # type: ignore[arg-type]
        assert excinfo.value.fix


class TestBackfillEmbeddings:
    @pytest.mark.asyncio
    async def test_disabled_capability_raises_before_touching_db(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[embedding]\nenabled = false\n", encoding="utf-8")
        cfg = load_config(str(path))
        with pytest.raises(ConfigError, match="未启用"):
            # 开关校验在选行之前，因此 session=None 不会被使用
            await service.backfill_embeddings(None, cfg)  # type: ignore[arg-type]

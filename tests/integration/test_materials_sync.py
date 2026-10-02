"""素材索引同步的集成用例（S2.4）：跑在 S2.9 的真实 Postgres 基座上。"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import PROJECT_ROOT
from xhs_agent.db import Base
from xhs_agent.db.models import Material as MaterialRow
from xhs_agent.services import materials as service
from xhs_agent.services.materials import sync_materials
from xhs_agent.tools import materials as materials_tool

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _Cfg:
    """轻量配置替身：同步服务只用到 materials_dir / index_dir 两个取路径的方法。"""

    def __init__(self, materials_dir, index_dir) -> None:
        self._materials = str(materials_dir)
        self._index = str(index_dir)

    def materials_dir(self) -> str:
        return self._materials

    def index_dir(self) -> str:
        return self._index


class FakeVision:
    """假视觉打标：只记调用次数，不联网。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, _frames, hint):
        self.calls.append(hint)
        return {"title": f"vision-{hint}", "tags": ["模型标签"], "description": "模型描述"}


def _png(width: int = 1080, height: int = 1920) -> bytes:
    body = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    return body + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8


def _make_pack(tmp_path: Path) -> Path:
    """假素材包：2 图 + 1 视频（带旁车）+ 1 个隐藏文件（应被忽略）。"""
    root = tmp_path / "materials"
    root.mkdir()
    (root / "球场-挥拍.mp4").write_bytes(b"\x00" * 64)
    (root / "球场-挥拍.mp4.txt").write_text("标题: 球场热身\n标签: 羽毛球, 挥拍\n描述: 清晨球场\n",
                                            encoding="utf-8")
    (root / "宠物猫.png").write_bytes(_png())
    (root / "晨跑-公园.png").write_bytes(_png(720, 1280))
    (root / ".hidden.mp4").write_bytes(b"\x00" * 8)
    return root


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    """每个用例前重建表结构（正式建表走 Alembic，这里用 metadata 快速搭台）。"""
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def fake_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """把抽帧替换成造一个假 jpg：让视觉打标路径在离线/无 ffmpeg 时也能被测到。"""

    def fake_extract(_path, out_dir, count=3, max_width=720, prefix="kf"):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        target = Path(out_dir) / f"{prefix}_0.jpg"
        target.write_bytes(b"\xff\xd8\xff")
        return [str(target)]

    monkeypatch.setattr(materials_tool.media, "extract_keyframes", fake_extract)


@pytest.fixture(autouse=True)
def keyframes_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把「工程根」指到临时的 tmp_path：用例的关键帧不出仓库，仍按相对路径入库。"""
    monkeypatch.setattr(service, "PROJECT_ROOT", str(tmp_path))


async def _rows(session: AsyncSession) -> list[MaterialRow]:
    return list((await session.execute(select(MaterialRow))).scalars().all())


class TestFirstSync:
    async def test_adds_all_media_files_and_stores_relative_keyframes(
            self, db_session: AsyncSession, tmp_path: Path, fake_frames: None) -> None:
        pack = _make_pack(tmp_path)
        vision = FakeVision()
        report = await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"), vision=vision)

        assert (report.scanned, report.added, report.updated, report.deleted) == (3, 3, 0, 0)
        rows = await _rows(db_session)
        assert len(rows) == 3

        video = next(row for row in rows if row.path.endswith(".mp4"))
        assert (video.source, video.title) == ("sidecar", "球场热身")
        # 有旁车描述的两条（视频）不打标，两张图片各打一次
        assert len(vision.calls) == 2

        keyframed = [row for row in rows if row.keyframes]
        assert keyframed
        for row in keyframed:
            for relative in row.keyframes:
                assert not os.path.isabs(relative)
                assert os.path.exists(os.path.join(tmp_path, relative))

    async def test_rerun_is_all_unchanged_and_never_calls_vision(
            self, db_session: AsyncSession, tmp_path: Path, fake_frames: None) -> None:
        pack = _make_pack(tmp_path)
        cfg = _Cfg(pack, tmp_path / "idx")
        await sync_materials(db_session, cfg, vision=FakeVision())

        second = FakeVision()
        report = await sync_materials(db_session, cfg, vision=second)
        assert (report.added, report.updated, report.deleted, report.unchanged) == (0, 0, 0, 3)
        assert second.calls == []

    async def test_does_not_write_legacy_json_index(
            self, db_session: AsyncSession, tmp_path: Path, fake_frames: None) -> None:
        pack = _make_pack(tmp_path)
        index_dir = tmp_path / "idx"
        await sync_materials(db_session, _Cfg(pack, index_dir))
        json_files = list(index_dir.glob("materials-*.json")) if index_dir.exists() else []
        assert json_files == []


class TestIncrementalChanges:
    async def test_new_file_is_added(self, db_session: AsyncSession, tmp_path: Path,
                                     fake_frames: None) -> None:
        pack = _make_pack(tmp_path)
        cfg = _Cfg(pack, tmp_path / "idx")
        await sync_materials(db_session, cfg)
        (pack / "新增-镜头.mp4").write_bytes(b"\x00" * 32)
        report = await sync_materials(db_session, cfg)
        assert (report.added, report.updated, report.unchanged) == (1, 0, 3)
        assert (await db_session.execute(
            select(func.count()).select_from(MaterialRow))).scalar_one() == 4

    async def test_changed_file_is_updated_and_clears_embedding(
            self, db_session: AsyncSession, tmp_path: Path, fake_frames: None) -> None:
        pack = _make_pack(tmp_path)
        cfg = _Cfg(pack, tmp_path / "idx")
        await sync_materials(db_session, cfg)

        target = pack / "宠物猫.png"
        await db_session.execute(text(
            "UPDATE materials SET embedding = array_fill(0.0::real, ARRAY[1024])::vector, "
            "embedding_model = 'fake-model' WHERE path = :p"), {"p": str(target)})
        await db_session.commit()

        target.write_bytes(_png(640, 640) + b"changed")  # size 变 → fingerprint 变
        report = await sync_materials(db_session, cfg)
        assert (report.updated, report.unchanged, report.added) == (1, 2, 0)

        row = (await db_session.execute(
            select(MaterialRow).where(MaterialRow.path == str(target)))).scalar_one()
        assert row.embedding is None
        assert row.embedding_model is None

    async def test_deleted_file_row_is_removed(self, db_session: AsyncSession, tmp_path: Path,
                                               fake_frames: None) -> None:
        pack = _make_pack(tmp_path)
        cfg = _Cfg(pack, tmp_path / "idx")
        await sync_materials(db_session, cfg)

        (pack / "晨跑-公园.png").unlink()
        report = await sync_materials(db_session, cfg)
        assert report.deleted == 1
        assert (await db_session.execute(
            select(func.count()).select_from(MaterialRow))).scalar_one() == 2
        gone = (await db_session.execute(select(MaterialRow).where(
            MaterialRow.path == str(pack / "晨跑-公园.png")))).scalar_one_or_none()
        assert gone is None

    async def test_sidecar_only_edit_counts_as_unchanged(
            self, db_session: AsyncSession, tmp_path: Path, fake_frames: None) -> None:
        """已知口径：fingerprint 只看素材文件本身，改旁车不改素材 → 判定未变。"""
        pack = _make_pack(tmp_path)
        cfg = _Cfg(pack, tmp_path / "idx")
        await sync_materials(db_session, cfg)

        (pack / "球场-挥拍.mp4.txt").write_text("标题: 新标题\n标签: 新标签\n描述: 新说明\n",
                                                encoding="utf-8")
        report = await sync_materials(db_session, cfg)
        assert (report.added, report.updated, report.deleted, report.unchanged) == (0, 0, 0, 3)
        video = next(row for row in await _rows(db_session) if row.path.endswith(".mp4"))
        assert video.title == "球场热身"  # 仍旧值，旁车改动不触发重建


class TestOpsCommand:
    async def test_cli_smoke(self, db_session: AsyncSession, tmp_path: Path,
                             postgres_dsn: str) -> None:
        pack = _make_pack(tmp_path)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            "[paths]\n"
            f'materials_dir = "{pack.as_posix()}"\n'
            f'index_dir = "{(tmp_path / "idx").as_posix()}"\n',
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["DATABASE_URL"] = postgres_dsn
        env["XHS_CONFIG_PATH"] = str(cfg_path)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "scripts/index_materials.py"],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert proc.returncode == 0, proc.stderr
        assert "新增 3" in proc.stdout
        assert (await db_session.execute(
            select(func.count()).select_from(MaterialRow))).scalar_one() == 3

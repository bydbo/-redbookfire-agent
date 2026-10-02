"""向量回填的集成用例（S2.5）：真容器 + 假 embedder，全程不联网。"""

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

from xhs_agent.config import PROJECT_ROOT, AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM
from xhs_agent.db.models import Material as MaterialRow
from xhs_agent.services.materials import backfill_embeddings, sync_materials
from xhs_agent.tools import materials as materials_tool
from xhs_agent.tools.embedding import EmbeddingError, EmbeddingResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "text-embedding-v3"


class _Cfg:
    """同步用的轻量配置替身：只提供 materials_dir / index_dir。"""

    def __init__(self, materials_dir, index_dir) -> None:
        self._materials = str(materials_dir)
        self._index = str(index_dir)

    def materials_dir(self) -> str:
        return self._materials

    def index_dir(self) -> str:
        return self._index


class FakeEmbedder:
    """假向量化：按文本生成可区分的单位向量；`fail_on` 指定第几批抛错。"""

    def __init__(self, fail_on: int | None = None) -> None:
        self.fail_on = fail_on
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.batches.append(list(texts))
        if self.fail_on is not None and len(self.batches) == self.fail_on:
            raise EmbeddingError("假失败：模拟上游不可用")
        vectors = []
        for content in texts:
            vector = [0.0] * EMBEDDING_DIM
            vector[0 if "近景" in content else 1] = 1.0
            vectors.append(vector)
        return EmbeddingResult(vectors=vectors, model=MODEL, prompt_tokens=len(texts),
                               attempts=1)


def _png(width: int = 1080, height: int = 1920) -> bytes:
    body = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
    return body + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8


def _make_pack(tmp_path: Path) -> Path:
    """假素材包：视频（带旁车）+ 两张图片，其中一张带可区分的标题。"""
    root = tmp_path / "materials"
    root.mkdir()
    (root / "球场-挥拍.mp4").write_bytes(b"\x00" * 64)
    (root / "球场-挥拍.mp4.txt").write_text("标题: 球场热身\n标签: 羽毛球\n描述: 清晨球场\n",
                                            encoding="utf-8")
    (root / "宠物猫.png").write_bytes(_png())
    (root / "近景-公园.png").write_bytes(_png(720, 1280))
    return root


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    """每个用例前重建表结构（正式建表走 Alembic）。"""
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def no_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """跳过真实抽帧：用例离线、不跑 ffmpeg。"""
    monkeypatch.setattr(materials_tool.media, "extract_keyframes",
                        lambda *_args, **_kwargs: [])


@pytest.fixture
def embedding_cfg(tmp_path: Path) -> AppConfig:
    """只声明 [embedding] 的临时配置：其余段用代码默认值。"""
    path = tmp_path / "config.toml"
    path.write_text(f'[embedding]\nenabled = true\nmodel = "{MODEL}"\n', encoding="utf-8")
    return load_config(str(path))


async def _rows(session: AsyncSession) -> list[MaterialRow]:
    return list((await session.execute(select(MaterialRow))).scalars().all())


async def _row_for(session: AsyncSession, path: Path) -> MaterialRow:
    return (await session.execute(
        select(MaterialRow).where(MaterialRow.path == str(path)))).scalar_one()


class TestBackfill:
    async def test_writes_vectors_and_model(self, db_session: AsyncSession, tmp_path: Path,
                                            no_frames: None,
                                            embedding_cfg: AppConfig) -> None:
        pack = _make_pack(tmp_path)
        await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"))

        embedder = FakeEmbedder()
        report = await backfill_embeddings(db_session, embedding_cfg, embedder=embedder)

        assert (report.pending, report.embedded, report.failed) == (3, 3, 0)
        assert report.model == MODEL
        assert report.batches == 1
        assert report.prompt_tokens == 3
        rows = await _rows(db_session)
        assert all(row.embedding is not None for row in rows)
        assert all(len(row.embedding or []) == EMBEDDING_DIM for row in rows)
        assert {row.embedding_model for row in rows} == {MODEL}
        assert len(embedder.batches) == 1 and len(embedder.batches[0]) == 3

    async def test_rerun_is_idempotent(self, db_session: AsyncSession, tmp_path: Path,
                                       no_frames: None, embedding_cfg: AppConfig) -> None:
        pack = _make_pack(tmp_path)
        await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"))
        await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())

        second = FakeEmbedder()
        report = await backfill_embeddings(db_session, embedding_cfg, embedder=second)
        assert (report.pending, report.embedded) == (0, 0)
        assert second.batches == []

    async def test_model_change_triggers_rebuild(self, db_session: AsyncSession, tmp_path: Path,
                                                 no_frames: None,
                                                 embedding_cfg: AppConfig) -> None:
        pack = _make_pack(tmp_path)
        await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"))
        await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())
        await db_session.execute(text("UPDATE materials SET embedding_model = 'old-model'"))
        await db_session.commit()

        report = await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())
        assert (report.pending, report.embedded) == (3, 3)
        rows = await _rows(db_session)
        assert {row.embedding_model for row in rows} == {MODEL}

    async def test_file_change_refills_only_that_row(self, db_session: AsyncSession,
                                                     tmp_path: Path, no_frames: None,
                                                     embedding_cfg: AppConfig) -> None:
        pack = _make_pack(tmp_path)
        cfg = _Cfg(pack, tmp_path / "idx")
        await sync_materials(db_session, cfg)
        await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())

        target = pack / "宠物猫.png"
        target.write_bytes(_png(640, 640) + b"changed")
        await sync_materials(db_session, cfg)
        assert (await _row_for(db_session, target)).embedding is None  # 内容变了 → 向量被清空

        report = await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())
        assert (report.pending, report.embedded) == (1, 1)

    async def test_failure_keeps_completed_batches_and_rerun_fills_rest(
            self, db_session: AsyncSession, tmp_path: Path, no_frames: None,
            embedding_cfg: AppConfig) -> None:
        pack = _make_pack(tmp_path)
        await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"))

        report = await backfill_embeddings(db_session, embedding_cfg,
                                           embedder=FakeEmbedder(fail_on=2), batch_size=2)
        assert (report.embedded, report.failed, report.batches) == (2, 1, 1)
        assert report.error
        rows = await _rows(db_session)
        assert sum(1 for row in rows if row.embedding is None) == 1
        assert (await db_session.execute(
            select(func.count()).select_from(MaterialRow)
            .where(MaterialRow.embedding.is_not(None)))).scalar_one() == 2

        again = await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())
        assert (again.pending, again.embedded, again.failed) == (1, 1, 0)

    async def test_cosine_distance_ordering_works(self, db_session: AsyncSession,
                                                  tmp_path: Path, no_frames: None,
                                                  embedding_cfg: AppConfig) -> None:
        """证明 pgvector 的 `<=>` 与向量列写入能连起来用（向量召回的基础）。"""
        pack = _make_pack(tmp_path)
        await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"))
        await backfill_embeddings(db_session, embedding_cfg, embedder=FakeEmbedder())

        literal = "[" + ",".join(["1.0"] + ["0.0"] * (EMBEDDING_DIM - 1)) + "]"
        nearest = (await db_session.execute(text(
            "SELECT path FROM materials ORDER BY embedding <=> CAST(:v AS vector) LIMIT 1"),
            {"v": literal})).scalar_one()
        assert nearest.endswith("近景-公园.png")


class TestOpsCommand:
    async def test_cli_smoke_on_empty_index(self, tmp_path: Path, postgres_dsn: str) -> None:
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(f'[embedding]\nenabled = true\nmodel = "{MODEL}"\n',
                            encoding="utf-8")
        proc = _run_cli(cfg_path, postgres_dsn)
        assert proc.returncode == 0, proc.stderr
        assert "待处理 0" in proc.stdout
        assert MODEL in proc.stdout

    async def test_cli_reports_upstream_failure(self, db_session: AsyncSession, tmp_path: Path,
                                                no_frames: None, postgres_dsn: str) -> None:
        pack = _make_pack(tmp_path)
        await sync_materials(db_session, _Cfg(pack, tmp_path / "idx"))
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            f'[embedding]\nenabled = true\nmodel = "{MODEL}"\n'
            'base_url = "http://127.0.0.1:9/v1"\nmax_retries = 0\ntimeout_s = 2\n',
            encoding="utf-8",
        )
        proc = _run_cli(cfg_path, postgres_dsn)
        assert proc.returncode == 1
        assert "失败 3" in proc.stdout


def _run_cli(cfg_path: Path, dsn: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    env["XHS_CONFIG_PATH"] = str(cfg_path)
    env["DASHSCOPE_API_KEY"] = "sk-test-value"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "scripts/backfill_embeddings.py"],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )

"""E9 隔离库运行器：Alembic 升级 → 索引 → oracle 四策略。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from xhs_agent.config import load_config
from xhs_agent.evaluation.experiment import index_dataset, run_split
from xhs_agent.evaluation.models import Case, Dataset, DatasetManifest, MaterialAsset
from xhs_agent.tools.embedding import EmbeddingResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += len(texts)
        return EmbeddingResult(vectors=[[1.0] + [0.0] * 1023 for _ in texts],
                               model="fake", prompt_tokens=len(texts))


def _config(tmp_path: Path, root: Path) -> tuple[Path, object]:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\nmaterials_dir = "{root.as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        f'index_dir = "{(tmp_path / "index").as_posix()}"\n'
        "[embedding]\nenabled = true\nmodel = \"text-embedding-v3\"\n"
        "[retrieval]\nsimilarity_threshold = 0.0\nmax_cosine_distance = 1.5\n"
        "[match]\ntopk = 5\n",
        encoding="utf-8")
    return config, load_config(str(config))


def _dataset(path: str) -> Dataset:
    data = PNG
    return Dataset(
        manifest=DatasetManifest(version="e9-test", protocol_version="1",
                                 dataset_kind="engineering_fake", status="frozen"),
        materials=[MaterialAsset(
            material_key="mat-alpha", relative_path=path,
            sha256=hashlib.sha256(data).hexdigest(), media_type="image",
            source="synthetic", license="CC0", model_transfer_allowed=True)],
        cases=[Case.model_validate({
            "case_id": "case-alpha", "group_id": "g1", "split": "dev",
            "input_type": "text", "natural_input": {"text": "alpha 早餐"},
            "expectation": {"answerable": True, "summary": "alpha"},
            "oracle": {"elements": [{"type": "topic", "value": "alpha"}],
                       "match_keywords": ["alpha"]},
            "scenario_tags": ["natural_naming"], "notes": "",
        })],
        qrels=[], reviews=[], e2e_tasks=[],
    )


async def _migrate(database_url: str, config_path: Path) -> None:
    env = {**os.environ, "DATABASE_URL": database_url,
           "XHS_CONFIG_PATH": str(config_path), "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    assert result.returncode == 0, result.stderr[-2000:]


async def test_index_and_oracle_runner_on_alembic_db(
        db_engine, postgres_dsn, tmp_path: Path) -> None:
    root = tmp_path / "experiment"
    root.mkdir()
    (root / "alpha.png").write_bytes(PNG)
    (root / "alpha.png.txt").write_text("标题: alpha 早餐\n标签: alpha\n", encoding="utf-8")
    config, cfg = _config(tmp_path, root)
    await _migrate(postgres_dsn, config)
    dataset = _dataset("alpha.png")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        payload = await index_dataset(session, cfg, dataset, root, FakeEmbedder(),
                                      root / "results")
        assert payload["materials_count"] == 1
        assert payload["digest"]
        attempts, index = await run_split(
            session, cfg, dataset, root, list(dataset.cases), split="dev",
            embedder=FakeEmbedder(), http=None, caller=None, modes=("oracle",))
    assert len(attempts) == 4
    assert {item.strategy for item in attempts} == {
        "keyword", "trgm_only", "vector_only", "hybrid"}
    assert all(item.status == "completed" for item in attempts)
    assert index["digest"] == payload["digest"]
    assert json.loads((root / "results" / "index.json").read_text(encoding="utf-8"))[
        "materials_count"] == 1

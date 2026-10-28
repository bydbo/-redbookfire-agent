"""E9 四策略编排：共享查询向量、单策略失败隔离与索引摘要。"""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import xhs_agent.evaluation.retrieval as runner
from xhs_agent.evaluation.experiment import library_digest
from xhs_agent.evaluation.models import Case, Dataset, DatasetManifest, MaterialAsset
from xhs_agent.schemas import Material
from xhs_agent.tools.embedding import EmbeddingResult


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(vectors=[[0.0] * 1024], model="fake", prompt_tokens=1)


def _dataset() -> Dataset:
    return Dataset(
        manifest=DatasetManifest(version="v", protocol_version="1",
                                 dataset_kind="engineering_fake", status="draft"),
        materials=[MaterialAsset(
            material_key="mat-a", relative_path="assets/a.jpg", sha256="a" * 64,
            media_type="image", source="s", license="CC0", model_transfer_allowed=True)],
        cases=[Case.model_validate({
            "case_id": "case-a", "group_id": "g", "split": "dev", "input_type": "text",
            "natural_input": {"text": "早餐"}, "expectation": {"answerable": True,
            "summary": "早餐"}, "oracle": {"elements": [{"type": "topic", "value": "早餐"}],
            "match_keywords": ["早餐"]}, "scenario_tags": ["natural_naming"], "notes": "",
        })],
        qrels=[], reviews=[], e2e_tasks=[],
    )


@pytest.mark.asyncio
async def test_oracle_shares_one_vector_across_vector_and_hybrid(monkeypatch) -> None:
    calls: list[tuple[tuple[str, ...] | None, list[float] | None]] = []

    async def fake_load(session: Any, dataset: Dataset):
        material = Material(id="uuid-a", path="assets/a.jpg", title="早餐",
                            description="", tags=[], mtime=0.0,
                            indexed_at="2026-10-28T00:00:00+08:00")
        return [material], {"uuid-a": "mat-a"}

    async def fake_retrieve(session: Any, cfg: Any, clue: Any, *, embedder: Any,
                            now: Any, channels: Any, query_vector: Any):
        calls.append((channels, query_vector))
        return SimpleNamespace(candidates=[SimpleNamespace(material_id="uuid-a")],
                               index_summary={"digest": "idx"})

    monkeypatch.setattr(runner, "load_library", fake_load)
    monkeypatch.setattr(runner, "retrieve_candidates", fake_retrieve)
    cfg = SimpleNamespace(match=SimpleNamespace(topk=5))
    embedder = FakeEmbedder()
    attempts, index = await runner.run_retrieval(
        object(), cfg, _dataset(), _dataset().cases, embedder=embedder,
        clue_mode="oracle", repeats=1)

    assert [item.strategy for item in attempts] == [
        "keyword", "trgm_only", "vector_only", "hybrid"]
    assert all(item.status == "completed" for item in attempts)
    assert embedder.calls == 1
    assert [c[0] for c in calls] == [
        ("literal",), ("vector",), ("literal", "vector")]
    vectors = [c[1] for c in calls if c[1] is not None]
    assert len(vectors) == 2 and vectors[0] == vectors[1]
    assert index == {"digest": "idx"}


def test_library_digest_is_stable_and_sensitive_to_identity() -> None:
    material_id = uuid.uuid4()
    row = SimpleNamespace(id=material_id, path="assets/a.jpg", fingerprint="fp",
                          indexed_at=dt.datetime(2026, 10, 28, tzinfo=dt.UTC),
                          embedding_model="m", embedding=[0.1])
    first = library_digest([row])
    assert first == library_digest([row])
    row.path = "assets/b.jpg"
    assert library_digest([row]) != first

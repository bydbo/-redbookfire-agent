"""素材检索服务的集成用例（S2.6）：真容器 + 假 embedder，全程不联网。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM
from xhs_agent.schemas import Element, HotspotClue
from xhs_agent.services.retrieval import retrieve_candidates
from xhs_agent.tools.embedding import EmbeddingError, EmbeddingResult

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)   # 查询向量（与假 embedder 一致）
E1 = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)  # 与查询向量正交 → 余弦距离 1.0


class FakeEmbedder:
    """假向量化：永远返回 E0，记录调用次数。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        return EmbeddingResult(vectors=[list(E0) for _ in texts], model=MODEL,
                               prompt_tokens=1, attempts=1)


class FailingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        raise EmbeddingError("假失败：上游不可用")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def write_config(tmp_path: Path, *, retrieval: dict | None = None, match: dict | None = None,
                 embedding: dict | None = None) -> AppConfig:
    """写一份临时 config.toml（默认取契约默认值），返回 `AppConfig`。"""
    sections = {
        "retrieval": {"similarity_threshold": 0.2, "recall_limit": 50,
                      "max_cosine_distance": 0.35, "rrf_k": 60, "w_element": 0.7,
                      "w_rrf": 0.3, "hit_threshold": 0.5},
        "match": {"topk": 5, "min_score": 0.15},
        "embedding": {"enabled": True, "model": MODEL},
    }
    sections["retrieval"].update(retrieval or {})
    sections["match"].update(match or {})
    sections["embedding"].update(embedding or {})

    lines: list[str] = []
    for name in ("retrieval", "match", "embedding"):
        lines.append(f"[{name}]")
        lines += [f"{key} = {_toml_value(value)}" for key, value in sections[name].items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_config(str(path))


def clue_with(elements, *, keywords=None, raw: str = "羽毛球球场随手拍") -> HotspotClue:
    """构造线索；`keywords` 默认取要素值（与 `HotspotClue.from_dict` 的兜底一致）。"""
    element_models = [Element(type=etype, value=value, weight=1.0, confidence=1.0)
                      for etype, value in elements]
    return HotspotClue(hotspot_raw=raw, why_it_works=["门槛低"],
                       mechanisms=[], elements=element_models,
                       match_keywords=list(keywords if keywords is not None
                                           else [value for _etype, value in elements]))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed(session: AsyncSession, *, path: str, title: str = "", description: str = "",
               tags: list[str] | None = None, elements: list[dict] | None = None,
               embedding: list[float] | None = None, model: str = MODEL) -> str:
    """直接插一条素材行（绕过文件扫描），返回数据库 uuid 字符串。"""
    literal = None if embedding is None else "[" + ",".join(repr(float(v)) for v in embedding) + "]"
    return (await session.execute(text(
        "INSERT INTO materials (path, type, title, description, tags, elements, source, "
        "duration_s, width, height, has_audio, size_bytes, fingerprint, embedding, embedding_model) "
        "VALUES (:path, 'video', :title, :description, CAST(:tags AS text[]), "
        "CAST(:elements AS jsonb), 'sidecar', 0, 1080, 1920, false, 0, :fingerprint, "
        "CAST(:embedding AS vector), :model) RETURNING id::text"
    ), {"path": path, "title": title, "description": description, "tags": list(tags or []),
        "elements": json.dumps(elements or [], ensure_ascii=False), "fingerprint": f"fp-{path}",
        "embedding": literal, "model": model if embedding is not None else None})).scalar_one()


async def seed_hit(session: AsyncSession, path: str, **overrides) -> str:
    """一条会被字面通道召回、也可能带向量的素材。"""
    base = {"title": "羽毛球球场", "description": "羽毛球球场挥拍", "tags": ["羽毛球"]}
    base.update(overrides)
    return await seed(session, path=path, **base)


async def seed_miss(session: AsyncSession, path: str, **overrides) -> str:
    """一条与线索字面无关的素材（文案换成咖啡）。"""
    base = {"title": "咖啡手冲", "description": "挂耳咖啡"}
    base.update(overrides)
    return await seed(session, path=path, **base)


class TestRecallChannels:
    async def test_literal_channel_recalls_text_match(self, db_session: AsyncSession,
                                                      tmp_path: Path) -> None:
        hit = await seed_hit(db_session, "D:/m/a.mp4")
        await seed_miss(db_session, "D:/m/b.mp4", embedding=E1)
        outcome = await retrieve_candidates(db_session, write_config(tmp_path),
                                            clue_with([("topic", "羽毛球")],
                                                      keywords=["羽毛球", "球场"]),
                                            embedder=FakeEmbedder())
        assert [c.material_id for c in outcome.candidates] == [hit]
        assert outcome.candidates[0].recall_sources == ["literal"]
        assert outcome.literal_recalled == 1
        assert outcome.vector_recalled == 0

    async def test_vector_channel_recalls_semantic_match(self, db_session: AsyncSession,
                                                         tmp_path: Path) -> None:
        conv = await seed_miss(db_session, "D:/m/c.mp4", embedding=E0)
        outcome = await retrieve_candidates(db_session, write_config(tmp_path),
                                            clue_with([("topic", "羽毛球")]), embedder=FakeEmbedder())
        assert [c.material_id for c in outcome.candidates] == [conv]
        assert outcome.candidates[0].recall_sources == ["vector"]
        assert outcome.literal_recalled == 0
        assert outcome.vector_recalled == 1

    async def test_both_channels_are_recorded(self, db_session: AsyncSession,
                                              tmp_path: Path) -> None:
        both = await seed_hit(db_session, "D:/m/d.mp4", embedding=E0)
        outcome = await retrieve_candidates(db_session, write_config(tmp_path),
                                            clue_with([("topic", "羽毛球")],
                                                      keywords=["羽毛球", "球场"]),
                                            embedder=FakeEmbedder())
        assert [c.material_id for c in outcome.candidates] == [both]
        assert outcome.candidates[0].recall_sources == ["literal", "vector"]
        assert outcome.candidates[0].to_dict()["recall_sources"] == ["literal", "vector"]

    async def test_similarity_threshold_gates_literal_channel(self, db_session: AsyncSession,
                                                              tmp_path: Path) -> None:
        await seed_hit(db_session, "D:/m/a.mp4")
        clue = clue_with([("topic", "羽毛球")], keywords=["羽毛球", "球场"])
        strict = write_config(tmp_path, retrieval={"similarity_threshold": 0.5},
                              embedding={"enabled": False})
        outcome = await retrieve_candidates(db_session, strict, clue)
        assert outcome.literal_recalled == 0
        assert outcome.candidates == []

    async def test_recall_limit_truncates_channel(self, db_session: AsyncSession,
                                                  tmp_path: Path) -> None:
        for index in range(3):
            await seed_hit(db_session, f"D:/m/n{index}.mp4")
        cfg = write_config(tmp_path, retrieval={"recall_limit": 1},
                           embedding={"enabled": False})
        outcome = await retrieve_candidates(db_session, cfg, clue_with([("topic", "羽毛球")],
                                                                       keywords=["羽毛球", "球场"]))
        assert outcome.literal_recalled == 1
        assert outcome.candidates_considered == 1
        assert len(outcome.candidates) == 1

    async def test_max_cosine_distance_gates_vector_channel(self, db_session: AsyncSession,
                                                            tmp_path: Path) -> None:
        await seed_miss(db_session, "D:/m/c.mp4", embedding=E1)  # 与查询向量正交 → 距离 1.0
        clue = clue_with([("topic", "羽毛球")])
        blocked = await retrieve_candidates(
            db_session, write_config(tmp_path, retrieval={"max_cosine_distance": 0.35}),
            clue, embedder=FakeEmbedder())
        assert blocked.vector_recalled == 0
        allowed = await retrieve_candidates(
            db_session, write_config(tmp_path, retrieval={"max_cosine_distance": 1.5}),
            clue, embedder=FakeEmbedder())
        assert allowed.vector_recalled == 1

    async def test_embedding_model_mismatch_is_excluded_from_vector_channel(
            self, db_session: AsyncSession, tmp_path: Path) -> None:
        current = await seed_miss(db_session, "D:/m/new.mp4", embedding=E0)
        await seed_miss(db_session, "D:/m/old.mp4", embedding=E0, model="old-model")
        outcome = await retrieve_candidates(db_session, write_config(tmp_path),
                                            clue_with([("topic", "羽毛球")]),
                                            embedder=FakeEmbedder())
        assert [c.material_id for c in outcome.candidates] == [current]
        assert outcome.vector_coverage.total == 2
        assert outcome.vector_coverage.with_embedding == 1


class TestFusionAndRanking:
    async def test_rrf_boosts_material_recalled_by_both_channels(self, db_session: AsyncSession,
                                                                 tmp_path: Path) -> None:
        text_only = await seed_hit(db_session, "D:/m/p1.mp4")
        both = await seed_hit(db_session, "D:/m/p2.mp4", embedding=E0)
        clue = clue_with([("topic", "羽毛球")], keywords=["羽毛球", "球场"])
        outcome = await retrieve_candidates(db_session, write_config(tmp_path), clue,
                                            embedder=FakeEmbedder())
        scores = {c.material_id: c.score for c in outcome.candidates}
        assert scores[both] > scores[text_only]  # 双通道加成来自 RRF 项

        flat = await retrieve_candidates(
            db_session, write_config(tmp_path, retrieval={"w_element": 1.0, "w_rrf": 0.0}),
            clue, embedder=FakeEmbedder())
        flat_scores = {c.material_id: c.score for c in flat.candidates}
        assert flat_scores[both] == flat_scores[text_only]  # 关掉 RRF 后同分

    async def test_topk_and_min_score_come_from_match_section(self, db_session: AsyncSession,
                                                              tmp_path: Path) -> None:
        for index in range(3):
            await seed_hit(db_session, f"D:/m/n{index}.mp4")
        clue = clue_with([("topic", "羽毛球")], keywords=["羽毛球", "球场"])
        topk_cfg = write_config(tmp_path, match={"topk": 2}, embedding={"enabled": False})
        outcome = await retrieve_candidates(db_session, topk_cfg, clue)
        assert [c.rank for c in outcome.candidates] == [1, 2]

        cut_cfg = write_config(tmp_path, match={"topk": 5, "min_score": 1.0},
                               embedding={"enabled": False})
        assert (await retrieve_candidates(db_session, cut_cfg, clue)).candidates == []

    async def test_hit_threshold_changes_coverage(self, db_session: AsyncSession,
                                                  tmp_path: Path) -> None:
        # 要素「羽毛球运动」对素材文案「羽毛球 咖啡手冲」的相似度 = 0.25（bigram Jaccard）
        await seed(db_session, path="D:/m/c.mp4", title="咖啡手冲", tags=["羽毛球"],
                   embedding=E0)
        clue = clue_with([("topic", "羽毛球运动")])
        loose = await retrieve_candidates(
            db_session, write_config(tmp_path, retrieval={"similarity_threshold": 0.9,
                                                          "hit_threshold": 0.2}),
            clue, embedder=FakeEmbedder())
        assert loose.coverage.ratio == 1.0
        strict = await retrieve_candidates(
            db_session, write_config(tmp_path, retrieval={"similarity_threshold": 0.9,
                                                          "hit_threshold": 0.5}),
            clue, embedder=FakeEmbedder())
        assert strict.coverage.ratio == 0.0
        assert [e.value for e in strict.coverage.gaps] == ["羽毛球运动"]

    async def test_scores_are_rounded_and_stable(self, db_session: AsyncSession,
                                                tmp_path: Path) -> None:
        await seed_hit(db_session, "D:/m/a.mp4", embedding=E0)
        clue = clue_with([("topic", "羽毛球")], keywords=["羽毛球", "球场"])
        cfg = write_config(tmp_path)
        first = await retrieve_candidates(db_session, cfg, clue, embedder=FakeEmbedder())
        second = await retrieve_candidates(db_session, cfg, clue, embedder=FakeEmbedder())
        assert all(c.score == round(c.score, 4) for c in first.candidates)
        assert [c.to_dict() for c in first.candidates] == [c.to_dict() for c in second.candidates]
        assert first.coverage.to_dict() == second.coverage.to_dict()


class TestVectorCoverageAndFailures:
    async def test_coverage_counts_current_model_vectors_only(self, db_session: AsyncSession,
                                                              tmp_path: Path) -> None:
        await seed_miss(db_session, "D:/m/a.mp4", embedding=E0)
        await seed_miss(db_session, "D:/m/b.mp4", embedding=E0, model="old-model")
        await seed_miss(db_session, "D:/m/c.mp4")
        outcome = await retrieve_candidates(db_session, write_config(tmp_path),
                                            clue_with([("topic", "羽毛球")]),
                                            embedder=FakeEmbedder())
        coverage = outcome.vector_coverage
        assert (coverage.total, coverage.with_embedding, coverage.enabled) == (3, 1, True)
        assert coverage.ratio == pytest.approx(1 / 3)
        assert coverage.to_dict()["model"] == MODEL

    async def test_disabled_embedding_runs_literal_only(self, db_session: AsyncSession,
                                                       tmp_path: Path) -> None:
        hit = await seed_hit(db_session, "D:/m/a.mp4")
        await seed_miss(db_session, "D:/m/c.mp4", embedding=E0)
        failure = FailingEmbedder()
        outcome = await retrieve_candidates(
            db_session, write_config(tmp_path, embedding={"enabled": False}),
            clue_with([("topic", "羽毛球")], keywords=["羽毛球", "球场"]), embedder=failure)
        assert [c.material_id for c in outcome.candidates] == [hit]
        assert outcome.candidates[0].recall_sources == ["literal"]
        assert failure.calls == 0  # 未启用就不该调用向量化
        assert outcome.vector_coverage.enabled is False
        assert outcome.vector_coverage.with_embedding == 0
        assert outcome.vector_coverage.ratio == 0.0

    async def test_query_embedding_failure_raises(self, db_session: AsyncSession,
                                                 tmp_path: Path) -> None:
        await seed_miss(db_session, "D:/m/c.mp4", embedding=E0)
        with pytest.raises(EmbeddingError):
            await retrieve_candidates(db_session, write_config(tmp_path),
                                      clue_with([("topic", "羽毛球")]),
                                      embedder=FailingEmbedder())

    async def test_empty_library_returns_gaps(self, db_session: AsyncSession,
                                              tmp_path: Path) -> None:
        outcome = await retrieve_candidates(db_session, write_config(tmp_path),
                                            clue_with([("topic", "羽毛球")]),
                                            embedder=FakeEmbedder())
        assert outcome.candidates == []
        assert outcome.coverage.ratio == 0.0
        assert [e.value for e in outcome.coverage.gaps] == ["羽毛球"]
        assert outcome.candidates_considered == 0

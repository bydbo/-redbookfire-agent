"""素材检索服务：双通道召回 + RRF 融合 + 要素加权（真库 I/O）。

用途：一次热点检索——通道 A（pg_trgm 字面）与通道 B（pgvector 余弦）各自召回候选，
      交给 `tools/retrieval` 融合与排序，返回契约终态候选、覆盖度与向量覆盖率。
输入：`AsyncSession`、`AppConfig`、`HotspotClue`；可注入 `embedder`（便于离线测试）与时间基准 `now`。
输出：`RetrievalOutcome`（候选 / 覆盖度 / 向量覆盖率 / 各通道召回数）。

边界（《检索契约》§三、§十）：
- 查询向量调用在重试后仍失败 → 直接抛 `EmbeddingError`，**不退回纯字面模式**；
- `[embedding].enabled = false` 属**能力裁剪**（同 vision）：只跑通道 A，向量覆盖率标 `enabled=false`，
  这也是 S2.8 纯字面基线的走法；
- 无向量的素材只参与通道 A；`embedding_model` 与当前模型不符的行不进通道 B，也不计入覆盖率分子；
- 候选集为空时正常返回空列表 + 全缺口覆盖度，不报错。
"""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from pgvector.sqlalchemy import Vector
from sqlalchemy import Text, bindparam, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..db.models import EMBEDDING_DIM
from ..db.models import Material as MaterialRow
from ..schemas import Coverage, HotspotClue, MatchCandidate
from ..tools import retrieval as retrieval_tool
from ..tools.embedding import EmbeddingClient, build_embedder
from ..tools.http import build_http_client
from .materials import row_to_material

# 通道 A 的文本表达式必须与数据契约 §4.1 的索引表达式逐字一致，
# 否则查询走不到 `ix_materials_text_trgm`（gin_trgm_ops）。
LITERAL_TEXT_SQL = "coalesce(title, '') || ' ' || coalesce(description, '')"


@dataclass
class VectorCoverage:
    """向量覆盖率：分母是库中全部素材，分子是「本次参与向量召回」的行（§三）。

    分子要求 `embedding` 非空**且** `embedding_model` 等于当前模型（混代向量不可比，§九）；
    `[embedding].enabled = false` 时分子记 0（本次没有素材参与向量召回），`total` 仍报库内总数。
    """

    enabled: bool = False
    model: str = ""
    total: int = 0
    with_embedding: int = 0
    ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "total": self.total,
            "with_embedding": self.with_embedding,
            "ratio": round(self.ratio, 4),
        }


@dataclass
class RetrievalOutcome:
    """一次检索的完整结果：候选、覆盖度、向量覆盖率与通道计数。"""

    candidates: list[MatchCandidate] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    vector_coverage: VectorCoverage = field(default_factory=VectorCoverage)
    literal_recalled: int = 0
    vector_recalled: int = 0
    candidates_considered: int = 0


async def literal_recall(session: AsyncSession, keywords: list[str], *, limit: int,
                         threshold: float) -> list[tuple[str, float]]:
    """通道 A：pg_trgm 字面召回，返回 `[(material_id, word_similarity), ...]`（得分降序）。

    度量是 `word_similarity(关键词, 素材文本)`：取「关键词 trigram 集合」与素材文本中任意连续
    片段的最大相似度，语义就是「关键词是否出现在素材文案里」。**不能用对称的 `similarity()`**：
    素材文案几十字、关键词只有 2–5 字时，分母被素材侧 trigram 主导，实测相似度仅 0.03–0.14，
    默认阈值 0.2 下召回恒为 0（实测见 `evals/reports/`，决策见 ADR 0010）。

    多关键词逐行取最大值：任一关键词过阈值即召回该素材。
    """
    cleaned = [item.strip() for item in keywords if item and item.strip()]
    if not cleaned:
        return []
    statement = text(
        f"SELECT m.id::text AS material_id, max(word_similarity(kw, {LITERAL_TEXT_SQL})) AS score "
        "FROM materials AS m "
        "CROSS JOIN unnest(CAST(:keywords AS text[])) AS kw "
        "GROUP BY m.id, m.path "
        f"HAVING max(word_similarity(kw, {LITERAL_TEXT_SQL})) > CAST(:threshold AS real) "
        "ORDER BY score DESC, m.path ASC LIMIT CAST(:limit AS integer)"
    ).bindparams(bindparam("keywords", type_=ARRAY(Text())))
    rows = (await session.execute(statement, {
        "keywords": cleaned, "threshold": threshold, "limit": limit})).all()
    return [(row.material_id, float(row.score)) for row in rows]


async def vector_recall(session: AsyncSession, query_vector: list[float], model: str, *,
                        limit: int, max_distance: float) -> list[tuple[str, float]]:
    """通道 B：pgvector 余弦最近邻，返回 `[(material_id, distance), ...]`（距离升序）。

    只取 `embedding_model` 等于当前模型的行——混代向量的距离不可比（§九）。
    """
    # 向量以 pgvector 的 `Vector` 类型绑定（不再手工 repr(float) 拼字面量）：序列化交给库，
    # 与 ORM 写 `materials.embedding` 的口径统一（S3.5）。
    statement = text(
        "SELECT id::text AS material_id, (embedding <=> CAST(:v AS vector)) AS distance "
        "FROM materials "
        "WHERE embedding IS NOT NULL AND embedding_model = CAST(:model AS text) "
        "AND (embedding <=> CAST(:v AS vector)) <= CAST(:max_distance AS real) "
        "ORDER BY distance ASC, path ASC LIMIT CAST(:limit AS integer)"
    ).bindparams(bindparam("v", type_=Vector(EMBEDDING_DIM)))
    rows = (await session.execute(statement,
                                  {"v": list(query_vector), "model": model,
                                   "max_distance": max_distance, "limit": limit})).all()
    return [(row.material_id, float(row.distance)) for row in rows]


async def vector_coverage(session: AsyncSession, cfg: AppConfig) -> VectorCoverage:
    """统计向量覆盖率：库中总行数 / 可参与向量召回的行数 / 比例 / 是否启用。"""
    row = (await session.execute(text(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE embedding IS NOT NULL "
        "AND embedding_model = CAST(:model AS text)) AS with_embedding "
        "FROM materials"
    ), {"model": cfg.embedding.model})).one()
    total = int(row.total or 0)
    enabled = bool(cfg.embedding.enabled)
    with_embedding = int(row.with_embedding or 0) if enabled else 0
    return VectorCoverage(
        enabled=enabled,
        model=cfg.embedding.model,
        total=total,
        with_embedding=with_embedding,
        ratio=(with_embedding / total) if (enabled and total) else 0.0,
    )


async def _load_materials(session: AsyncSession, material_ids: list[str]) -> list[MaterialRow]:
    if not material_ids:
        return []
    keys = [uuid.UUID(material_id) for material_id in material_ids]
    rows = (await session.execute(
        select(MaterialRow).where(MaterialRow.id.in_(keys)))).scalars().all()
    return list(rows)


def _ranks(hits: list[tuple[str, float]]) -> dict[str, int]:
    """SQL 已按相关性排序，序号即通道内 rank（从 1 开始）。"""
    return {material_id: rank for rank, (material_id, _score) in enumerate(hits, start=1)}


async def retrieve_candidates(session: AsyncSession, cfg: AppConfig, clue: HotspotClue, *,
                              embedder: EmbeddingClient | None = None,
                              now: float | None = None,
                              topk: int | None = None,
                              http: httpx.AsyncClient | None = None) -> RetrievalOutcome:
    """一次检索：双通道召回 → 映射成内存素材 → 融合排序 → 返回结果。

    输入：`session`、`cfg`（读 `[retrieval]` / `[match]` / `[embedding]`）、`clue`；
    `embedder`（可注入的向量化对象，None = 按配置现造）、`now`（时间基准，Unix 秒，可选）。
    `topk`（可选覆盖：worker 传 `runs.topk`；None = 用 `cfg.match.topk`）。
    `http`（可选 httpx 客户端：`embedder` 未注入时用它造向量客户端，让一次运行复用同一个
    连接池；None = 这里临时起一个）。
    异常：向量召回已启用但查询向量化失败时抛 `EmbeddingError`（不降级）。
    """
    params = retrieval_tool.RetrievalParams.from_config(cfg)
    if topk is not None:
        params = replace(params, topk=int(topk))   # RetrievalParams 是 frozen dataclass
    outcome = RetrievalOutcome(vector_coverage=await vector_coverage(session, cfg))

    literal_hits = await literal_recall(session, retrieval_tool.literal_query_keywords(clue),
                                        limit=params.recall_limit,
                                        threshold=params.similarity_threshold)
    outcome.literal_recalled = len(literal_hits)

    vector_hits: list[tuple[str, float]] = []
    if cfg.embedding.enabled:
        # 注入的 embedder 用自己的生命周期；没注入时起一个短生命周期客户端（独立调用路径）。
        async with AsyncExitStack() as stack:
            client: EmbeddingClient | None = embedder
            if client is None:
                shared = http
                if shared is None:
                    shared = await stack.enter_async_context(
                        build_http_client(cfg.embedding.timeout_s))
                client = build_embedder(cfg, client=shared)
            if client is None:  # pragma: no cover - enabled 为真时 build_embedder 不会返回 None
                raise RuntimeError("向量召回已启用但没有可用的 embedding 客户端")
            embedded = await client.embed([retrieval_tool.vector_query_text(clue)])
        query_vector = embedded.vectors[0]
        vector_hits = await vector_recall(session, query_vector, cfg.embedding.model,
                                          limit=params.recall_limit,
                                          max_distance=params.max_cosine_distance)
        outcome.vector_recalled = len(vector_hits)

    channel_ranks: dict[str, dict[str, int]] = {}
    if literal_hits:
        channel_ranks[retrieval_tool.LITERAL] = _ranks(literal_hits)
    if vector_hits:
        channel_ranks[retrieval_tool.VECTOR] = _ranks(vector_hits)

    material_ids = sorted({material_id for ranks in channel_ranks.values() for material_id in ranks})
    rows = await _load_materials(session, material_ids)
    order = {material_id: index for index, material_id in enumerate(material_ids)}
    materials = sorted((row_to_material(row) for row in rows),
                       key=lambda material: order.get(material.id, 0))
    outcome.candidates_considered = len(materials)
    outcome.candidates, outcome.coverage = retrieval_tool.rank_candidates(
        clue, materials, channel_ranks, params, now=now)
    return outcome

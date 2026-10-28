"""E9 检索运行器：四种策略共用同一素材库、同次线索与查询向量。

生产检索服务只增加显式通道 / 预计算向量两个可选入口；本模块负责把一次查询分派给
`keyword / trgm_only / vector_only / hybrid`，并记录每次尝试的排名、错误与阶段耗时。
oracle 每任务只执行一次；真实拆解每个 repeat 单独产生线索和查询向量。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..db.models import EMBEDDING_DIM
from ..db.models import Material as MaterialRow
from ..schemas import HotspotClue, Material
from ..services.materials import row_to_material
from ..services.retrieval import retrieve_candidates
from ..tools import retrieval as retrieval_tool
from ..tools.embedding import EmbeddingClient
from .models import STRATEGIES, Attempt, Case, Dataset, Strategy

ClueProvider = Callable[[Case, int], Awaitable[HotspotClue]]

STRATEGY_CHANNELS: dict[Strategy, tuple[str, ...] | None] = {
    "keyword": None,
    "trgm_only": (retrieval_tool.LITERAL,),
    "vector_only": (retrieval_tool.VECTOR,),
    "hybrid": (retrieval_tool.LITERAL, retrieval_tool.VECTOR),
}


def build_oracle_clue(case: Case) -> HotspotClue:
    """用人工确认的 oracle 构造线索，不读取任何模型结果。"""
    return HotspotClue.from_dict({
        "hotspot_raw": case.natural_input.text or case.expectation.summary,
        "elements": [element.model_dump() for element in case.oracle.elements],
        "match_keywords": list(case.oracle.match_keywords),
    })


def path_key_map(dataset: Dataset) -> dict[str, str]:
    """数据库 `materials.path`（相对素材根）→ 数据集 `material_key`。"""
    return {asset.relative_path.replace("\\", "/"): asset.material_key
            for asset in dataset.materials}


def _map_candidates(candidates: Sequence[Any], id_to_key: dict[str, str]) -> list[str]:
    keys: list[str] = []
    for candidate in candidates:
        material_id = str(candidate.material_id)
        key = id_to_key.get(material_id)
        if key is None:
            raise ValueError(f"检索返回了数据集之外的素材：{material_id}")
        keys.append(key)
    return keys


async def load_library(session: AsyncSession, dataset: Dataset) -> tuple[list[Material],
                                                                        dict[str, str]]:
    """读取已发布素材库并按 path 映射到数据集 key；缺素材直接失败。"""
    rows = list((await session.execute(
        select(MaterialRow).order_by(MaterialRow.path))).scalars().all())
    path_map = path_key_map(dataset)
    by_path = {row.path.replace("\\", "/"): row for row in rows}
    missing = [key for path, key in path_map.items() if path not in by_path]
    if missing:
        raise ValueError(f"索引缺少数据集素材：{missing[:5]}")
    materials = [row_to_material(row) for row in rows if row.path.replace("\\", "/") in path_map]
    id_to_key = {str(row.id): path_map[row.path.replace("\\", "/")]
                 for row in rows if row.path.replace("\\", "/") in path_map}
    return materials, id_to_key


async def _query_vector(embedder: EmbeddingClient, clue: HotspotClue) -> list[float]:
    result = await embedder.embed([retrieval_tool.vector_query_text(clue)])
    if not result.vectors or len(result.vectors[0]) != EMBEDDING_DIM:
        raise ValueError("查询向量为空或维度不符")
    return list(result.vectors[0])


async def run_retrieval(session: AsyncSession, cfg: AppConfig, dataset: Dataset,
                        cases: Sequence[Case], *, embedder: EmbeddingClient,
                        clue_mode: Literal["oracle", "real"] = "oracle", repeats: int = 1,
                        clue_provider: ClueProvider | None = None,
                        strategies: Sequence[Strategy] = STRATEGIES,
                        variant: str = "",
                        vector_cache: dict[tuple[str, int], list[float]] | None = None,
                        clue_cache: dict[tuple[str, int], HotspotClue] | None = None,
                        now: float | None = None) -> tuple[list[Attempt], dict[str, Any]]:
    """执行指定策略并返回全部尝试；单策略失败不影响其他策略留下证据。

    `variant` 用于 sweep 参数点，写进 attempt_id 避免同任务多参数点互相覆盖；
    `vector_cache` / `clue_cache` 让同一开发集的多个参数点共享查询向量和拆解结果。
    """
    unknown = [item for item in strategies if item not in STRATEGIES]
    if unknown:
        raise ValueError(f"未知检索策略：{unknown}")
    materials, id_to_key = await load_library(session, dataset)
    attempts: list[Attempt] = []
    index_summary: dict[str, Any] = {}
    need_vector = any(item in ("vector_only", "hybrid") for item in strategies)
    for case in cases:
        clue_rounds = 1 if clue_mode == "oracle" else repeats
        for repeat in range(1, clue_rounds + 1):
            cache_key = (case.case_id, repeat)
            clue = None
            if clue_cache is not None:
                clue = clue_cache.get(cache_key)
            if clue is None:
                if clue_mode == "oracle":
                    clue = build_oracle_clue(case)
                elif clue_provider is not None:
                    clue = await clue_provider(case, repeat)
                else:
                    raise ValueError("真实拆解模式需要注入 clue_provider")
                if clue_cache is not None:
                    clue_cache[cache_key] = clue
            vector: list[float] | None = None
            vector_error = ""
            embed_ms = 0
            if need_vector:
                started = time.perf_counter()
                cached = vector_cache.get(cache_key) if vector_cache is not None else None
                if cached is not None:
                    vector = list(cached)
                else:
                    try:
                        vector = await _query_vector(embedder, clue)
                        if vector_cache is not None:
                            vector_cache[cache_key] = list(vector)
                    except Exception as exc:  # 失败要记录，不能把字面策略也一起中断
                        vector_error = f"{type(exc).__name__}: {exc}"
                embed_ms = int((time.perf_counter() - started) * 1000)
            for strategy in strategies:
                suffix = f":{variant}" if variant else ""
                attempt_id = f"{case.case_id}:{clue_mode}:r{repeat}:{strategy}{suffix}"
                start = time.perf_counter()
                if strategy == "keyword":
                    ranked = retrieval_tool.keyword_substring_rank(clue, materials, cfg.match.topk)
                    keys = [id_to_key[material.id] for material in ranked]
                    attempts.append(Attempt(
                        attempt_id=attempt_id, case_id=case.case_id, repeat=repeat,
                        strategy=strategy, clue_mode=clue_mode,
                        status="completed", ranked_material_keys=keys,
                        stage_latency_ms={"retrieval": int((time.perf_counter() - start) * 1000)},
                        total_latency_ms=int((time.perf_counter() - start) * 1000),
                        input_digest=str(case.case_id)))
                    continue
                if strategy in ("vector_only", "hybrid") and vector_error:
                    attempts.append(Attempt(
                        attempt_id=attempt_id, case_id=case.case_id, repeat=repeat,
                        strategy=strategy, clue_mode=clue_mode,
                        status="failed", error=f"query embedding: {vector_error}",
                        stage_latency_ms={"embedding": embed_ms},
                        total_latency_ms=embed_ms))
                    continue
                try:
                    use_vector = strategy in ("vector_only", "hybrid")
                    outcome = await retrieve_candidates(
                        session, cfg, clue, embedder=embedder, now=now,
                        channels=STRATEGY_CHANNELS[strategy],
                        query_vector=vector if use_vector else None)
                    keys = _map_candidates(outcome.candidates, id_to_key)
                    elapsed = int((time.perf_counter() - start) * 1000)
                    attempts.append(Attempt(
                        attempt_id=attempt_id, case_id=case.case_id, repeat=repeat,
                        strategy=strategy, clue_mode=clue_mode,
                        status="completed", ranked_material_keys=keys,
                        stage_latency_ms={"embedding": embed_ms, "retrieval": elapsed},
                        total_latency_ms=embed_ms + elapsed,
                        input_digest=str(case.case_id)))
                    if not index_summary:
                        index_summary = dict(outcome.index_summary)
                except Exception as exc:
                    elapsed = int((time.perf_counter() - start) * 1000)
                    attempts.append(Attempt(
                        attempt_id=attempt_id, case_id=case.case_id, repeat=repeat,
                        strategy=strategy, clue_mode=clue_mode,
                        status="failed", error=f"{type(exc).__name__}: {exc}",
                        stage_latency_ms={"embedding": embed_ms, "retrieval": elapsed},
                        total_latency_ms=embed_ms + elapsed))
    return attempts, index_summary

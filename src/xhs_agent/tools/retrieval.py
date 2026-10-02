"""双通道召回的融合与最终排序（纯函数，不碰数据库、不联网、不看系统时钟）。

用途：把「通道 A 字面召回 + 通道 B 向量召回」的结果按《检索契约》§四／§五／§六 融合、
      加权、排序、截断，产出可直接落库与渲染的契约终态候选与覆盖度。
输入：`HotspotClue`、本次**召回集**（`schemas.Material` 列表）、每通道的 rank
      （`{"literal": {material_id: rank}, "vector": {...}}`）、`RetrievalParams`、时间基准 `now`。
输出：`(candidates, coverage)`——候选已补 `reasons` 并 `finalize()`，可直接流向报告与落库。
口径：RRF 是相对分（只在本次候选集内有意义）；同分按 `material_id` 升序；
      分数先 `round(..., 4)` 再比较，避免浮点尾差导致排序抖动。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..schemas import Coverage, HotspotClue, MatchCandidate, Material
from . import matching

LITERAL = "literal"
VECTOR = "vector"
# 通道顺序固定，保证 `recall_sources` 与测试断言稳定
CHANNELS = (LITERAL, VECTOR)
SCORE_PRECISION = 4


def _push(parts: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in parts:
        parts.append(text)


def literal_query_keywords(clue: HotspotClue) -> list[str]:
    """通道 A 的查询关键词：`match_keywords` + 全部要素 value（去重去空，§三）。

    逐关键词调用 `word_similarity`（ADR 0010）：关键词当「模式」、素材文案当「被查文本」，
    语义即「关键词是否出现在素材文案里」——不能用对称的 `similarity()`。
    """
    parts: list[str] = []
    for text in list(clue.match_keywords or []) + [element.value for element in clue.elements or []]:
        _push(parts, text)
    return parts


def literal_query_text(clue: HotspotClue) -> str:
    """通道 A 查询关键词的空格连接形式（保留给日志与测试断言）。"""
    return " ".join(literal_query_keywords(clue))


def vector_query_text(clue: HotspotClue) -> str:
    """通道 B 的查询文本：热点原文 → why_it_works → 各 mechanism 的 name / explain（§三）。

    改动这里的拼接内容会改变历史排序，属《检索契约》§九 的破坏性变更。
    """
    parts: list[str] = []
    _push(parts, clue.hotspot_raw)
    for why in clue.why_it_works or []:
        _push(parts, why)
    for mechanism in clue.mechanisms or []:
        _push(parts, mechanism.name)
        _push(parts, mechanism.explain)
    return " ".join(parts)


def rrf_scores(channel_ranks: Mapping[str, Mapping[str, int]], k: int) -> dict[str, float]:
    """RRF 融合分：`Σ_c 1 / (k + rank_c(m))`，`rank` 从 1 开始（§四）。"""
    scores: dict[str, float] = {}
    for ranks in channel_ranks.values():
        for material_id, rank in ranks.items():
            scores[material_id] = scores.get(material_id, 0.0) + 1.0 / (k + rank)
    return scores


def normalize_rrf(scores: Mapping[str, float]) -> dict[str, float]:
    """在本次候选集内按最大值归一化；全为 0（或空集）时全部记 0（§四）。"""
    top = max(scores.values()) if scores else 0.0
    if top <= 0:
        return dict.fromkeys(scores, 0.0)
    return {key: value / top for key, value in scores.items()}


@dataclass(frozen=True)
class RetrievalParams:
    """《检索契约》§八 的参数表（`[retrieval]` 与 `[match]` 两段配置的合并视图）。"""

    similarity_threshold: float = 0.2
    recall_limit: int = 50
    max_cosine_distance: float = 0.35
    rrf_k: int = 60
    w_element: float = 0.7
    w_rrf: float = 0.3
    hit_threshold: float = 0.5
    topk: int = 5
    min_score: float = 0.15
    element_type_weights: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> RetrievalParams:
        """从 `AppConfig` 取参：召回参数来自 `[retrieval]`，截断参数来自 `[match]`。"""
        return cls(
            similarity_threshold=cfg.retrieval.similarity_threshold,
            recall_limit=cfg.retrieval.recall_limit,
            max_cosine_distance=cfg.retrieval.max_cosine_distance,
            rrf_k=cfg.retrieval.rrf_k,
            w_element=cfg.retrieval.w_element,
            w_rrf=cfg.retrieval.w_rrf,
            hit_threshold=cfg.retrieval.hit_threshold,
            topk=cfg.match.topk,
            min_score=cfg.match.min_score,
            element_type_weights=dict(cfg.match.element_type_weights or {}),
        )


def final_score(element_score: float, rrf_norm: float, quality: float, recency: float,
                params: RetrievalParams) -> float:
    """§五 最终分：`clamp01((W_ELEMENT·要素相关度 + W_RRF·rrf_norm)·(0.9+0.1·画质) + 时效分)`。"""
    base = params.w_element * element_score + params.w_rrf * rrf_norm
    return max(0.0, min(1.0, base * (0.9 + 0.1 * quality) + recency))


def _coverage(clue: HotspotClue, coverage_map: Mapping[tuple[str, str], float],
              hit_threshold: float) -> Coverage:
    """覆盖度 = 命中要素权重之和 ÷ 全部线索要素权重之和（§七）。

    命中相似度沿用 `matching.element_relevance`，因此「线索级 `match_keywords` 命中不区分
    要素类型」这一**已知行为**会同期体现在覆盖度上——该行为已记在
    `tests/unit/test_matching.py::test_keyword_hit_is_not_type_aware`，收紧它属于排序口径变更，
    需单独评估（会改动检索契约里的打分结果）。
    """
    covered, gaps, covered_weight, total_weight = [], [], 0.0, 0.0
    for element in clue.elements or []:
        weight = element.score_weight
        total_weight += weight
        if coverage_map.get((element.type, element.value), 0.0) >= hit_threshold:
            covered.append(element)
            covered_weight += weight
        else:
            gaps.append(element)
    ratio = (covered_weight / total_weight) if total_weight else 0.0
    return Coverage(covered=covered, gaps=gaps, ratio=ratio)


def rank_candidates(clue: HotspotClue, materials: list[Material],
                    channel_ranks: Mapping[str, Mapping[str, int]],
                    params: RetrievalParams, *, now: float | None = None
                    ) -> tuple[list[MatchCandidate], Coverage]:
    """融合 + 要素加权 + 排序 + 截断，返回 `(终态候选列表, 覆盖度)`。

    输入：`materials` 是本次**召回集**（两个通道带回来的素材），`channel_ranks` 是各通道的
    rank；没有出现在任何通道里的素材会被跳过（不产出「无来源候选」）。
    输出：按 `score` 降序、同分 `material_id` 升序；低于 `min_score` 截止；每条候选补齐
    `reasons` / `usage`（规则版解释）并 `finalize()`。
    """
    rrf_map = normalize_rrf(rrf_scores(channel_ranks, params.rrf_k))
    coverage_map: dict[tuple[str, str], float] = {}
    scored: list[tuple[float, Material, list[Any], list[Any], list[str]]] = []

    for material in materials:
        sources = [name for name in CHANNELS if material.id in channel_ranks.get(name, {})]
        if not sources:
            continue
        matches = matching.match_elements(clue, material, params.element_type_weights)
        # 要素加权（§五）与命中/缺口（固定 0.5 门槛）都从同一批匹配结果派生
        total_weight = sum(item.weight for item in matches)
        gained = sum(item.contribution for item in matches)
        element_score = (gained / total_weight) if total_weight else 0.0
        hits, missing = matching.split_hits(matches)
        score = round(final_score(element_score, rrf_map.get(material.id, 0.0),
                                  material.quality_score,
                                  matching.recency_bonus(material, now), params),
                      SCORE_PRECISION)
        scored.append((score, material, hits, missing, sources))
        # 覆盖度用**未过滤**的相似度，判定阈值是 [retrieval].hit_threshold（§七）
        for item in matches:
            key = (item.element.type, item.element.value)
            coverage_map[key] = max(coverage_map.get(key, 0.0), item.similarity)

    scored.sort(key=lambda item: (-item[0], item[1].id))
    candidates: list[MatchCandidate] = []
    for rank, (score, material, hits, missing, sources) in enumerate(
            scored[:max(0, params.topk)], start=1):
        if score < params.min_score:
            break
        candidates.append(MatchCandidate.draft(
            material_id=material.id,
            material=material,
            score=score,
            recall_sources=sources,
            hits=hits,
            missing=missing,
            rank=rank,
        ))
    matching.explain_candidates(clue, candidates)
    return candidates, _coverage(clue, coverage_map, params.hit_threshold)

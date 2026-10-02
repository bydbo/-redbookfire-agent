"""热点线索 <-> 素材 的相关性打分。

用途：只用确定性算法把「哪些素材能蹭上这条热点线索」排出来，再交给 Agent 解释与取舍。
输入：`HotspotClue` + `Material` 列表，以及可选的要素类型权重与**时间基准**。
输出：候选列表（`MatchCandidate`）+ 覆盖度（`Coverage`），分数归一化到 0~1。

输出稳定性：分数里的「时效加减分」依赖当前时间，因此时间基准可以用 `now` 注入；
不注入时取 `time.time()`（默认行为不变）。同一输入 + 同一 `now` 必须得到同一结果。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..schemas import (
    DEFAULT_TYPE_WEIGHTS,
    Coverage,
    Element,
    ElementHit,
    HotspotClue,
    MatchCandidate,
    Material,
)
from ..util import normalize_text, text_similarity

HIT_THRESHOLD = 0.5
FRESH_DAYS = 30
STALE_DAYS = 365


def material_text(material: Material) -> str:
    """素材的可检索文本：标签 + 标题 + 描述。"""
    parts = list(material.tags or []) + [material.title or "", material.description or ""]
    return " ".join(p for p in parts if p)


@dataclass(frozen=True)
class ElementMatch:
    """一条线索要素在一条素材上的匹配结果（`similarity` **未做命中阈值过滤**）。"""

    element: Element
    similarity: float
    hit_value: str
    weight: float
    contribution: float


def match_elements(clue: HotspotClue, material: Material,
                   type_weights: dict | None = None) -> list[ElementMatch]:
    """逐要素比对，返回每条线索要素的最佳匹配（顺序与 `clue.elements` 一致）。

    输入：`clue`（线索要素 + 命中关键词）、`material`（标签/标题/描述/要素）、
    `type_weights`（覆盖默认要素类型权重，可选）。
    输出：`ElementMatch` 列表——`weight = element.score_weight × 类型权重`，
    `contribution = weight × similarity`。**不做命中阈值过滤**，让调用方按自己的阈值判定
    （打分用加权平均，覆盖度用 `[retrieval].hit_threshold`）。
    """
    weights = dict(DEFAULT_TYPE_WEIGHTS)
    weights.update(type_weights or {})
    corpus = material_text(material)
    material_elements = material.elements or []

    matches: list[ElementMatch] = []
    for element in clue.elements:
        type_weight = weights.get(element.type, 0.5)
        weight = element.score_weight * type_weight

        best_sim = 0.0
        best_value = ""
        for candidate in material_elements:
            if candidate.type != element.type:
                continue
            sim = text_similarity(element.value, candidate.value)
            if sim > best_sim:
                best_sim, best_value = sim, candidate.value

        text_sim = text_similarity(element.value, corpus)
        if text_sim > best_sim:
            best_sim, best_value = text_sim, "素材文案命中"

        for surface in [element.evidence] + list(clue.match_keywords or []):
            surface = (surface or "").strip()
            if len(surface) < 2:
                continue
            if normalize_text(surface) and normalize_text(surface) in normalize_text(corpus):
                if best_sim < 0.9:
                    best_sim, best_value = 0.9, f"关键词「{surface}」命中"
                break

        contribution = weight * best_sim
        matches.append(ElementMatch(element=element, similarity=best_sim, hit_value=best_value,
                                    weight=weight, contribution=contribution))
    return matches


def split_hits(matches: list[ElementMatch]) -> tuple[list, list]:
    """按 `HIT_THRESHOLD` 把匹配结果分成命中与缺口（`hits` / `missing` 的固定口径）。"""
    hits = [ElementHit(element_type=m.element.type, clue_value=m.element.value,
                       hit_value=m.hit_value, similarity=m.similarity,
                       contribution=m.contribution)
            for m in matches if m.similarity >= HIT_THRESHOLD]
    missing = [m.element for m in matches if m.similarity < HIT_THRESHOLD]
    return hits, missing


def element_relevance(clue: HotspotClue, material: Material,
                      type_weights: dict | None = None) -> tuple:
    """要素级相关度：`Σ(w·c·t·sim) / Σ(w·c·t)`。

    输入：`clue`、`material`、`type_weights`（可选）。
    输出：`(relevance, hits, missing)`——relevance 归一化到 0~1；分母只统计线索侧要素，
    与候选集大小无关，因此是**绝对分、跨查询可比**（《检索契约》§五）。
    """
    matches = match_elements(clue, material, type_weights)
    total_weight = sum(item.weight for item in matches)
    gained = sum(item.contribution for item in matches)

    relevance = gained / total_weight if total_weight else 0.0
    hits, missing = split_hits(matches)
    return relevance, hits, missing


def score_material(clue: HotspotClue, material: Material, type_weights: dict | None = None,
                   now: float | None = None) -> tuple:
    """给单条素材打分（**要素加权口径**，legacy 要素级排序用）。

    输入：`clue`、`material`、`type_weights`（可选）、`now`（时间基准，Unix 秒，可选）。
    输出：`(score, hits, missing)`——score = clamp01(element_relevance·(0.9+0.1·quality) + 时效分)。
    注意：这是 `W_ELEMENT = 1` 的特例；含 RRF 融合的契约终分见 `tools/retrieval.py`。
    """
    relevance, hits, missing = element_relevance(clue, material, type_weights)
    score = relevance * (0.9 + 0.1 * material.quality_score)
    score += recency_bonus(material, now)
    return max(0.0, min(1.0, score)), hits, missing


def recency_bonus(material: Material, now: float | None = None) -> float:
    """时效加减分：30 天内 +0.02，一年以上 -0.03，其余 0；`mtime` 为空时不加减。"""
    if not material.mtime:
        return 0.0
    reference = time.time() if now is None else now
    days = (reference - material.mtime) / 86400
    if days <= FRESH_DAYS:
        return 0.02
    if days >= STALE_DAYS:
        return -0.03
    return 0.0


def rank_materials(clue: HotspotClue, materials: list, topk: int = 5, min_score: float = 0.0,
                   type_weights: dict | None = None, now: float | None = None) -> tuple:
    """给所有素材打分排序，返回 (候选列表, 覆盖度)。**legacy**：不含双通道 RRF。

    生产路径请用 `tools.retrieval.rank_candidates`（《检索契约》§二／§五 的终分口径）；
    本函数保留给要素级回归用例与 S2.7 之前的离线链路，清理离线链路时一并评估删除。

    输入：`materials` 列表、截断参数 `topk` / `min_score`，以及可选的 `type_weights` 与 `now`。
    输出：候选按分数降序、同分按 `path` 升序；低于 `min_score` 的候选直接截止（后面的也不再取）；
    覆盖度 = 被命中要素的权重之和 ÷ 全部要素权重之和。
    """
    scored = []
    coverage_map: dict = {}

    for material in materials:
        score, hits, missing = score_material(clue, material, type_weights, now)
        scored.append((score, hits, missing, material))
        for element in clue.elements:
            best = 0.0
            for hit in hits:
                if hit.element_type == element.type and hit.clue_value == element.value:
                    best = max(best, hit.similarity)
            key = (element.type, element.value)
            coverage_map[key] = max(coverage_map.get(key, 0.0), best)

    scored.sort(key=lambda item: (-item[0], item[3].path))
    candidates = []
    for rank, (score, hits, missing, material) in enumerate(scored[:max(0, topk)], start=1):
        if score < min_score:
            break
        # 候选先以中间态产出（reasons 由 explain_candidates 补齐），再 finalize 成契约终态。
        candidates.append(MatchCandidate.draft(
            material_id=material.id,
            material=material,
            score=score,
            hits=hits,
            missing=missing,
            rank=rank,
        ))

    covered, gaps, covered_weight, total_weight = [], [], 0.0, 0.0
    for element in clue.elements:
        weight = max(0.05, element.weight * element.confidence)
        total_weight += weight
        if coverage_map.get((element.type, element.value), 0.0) >= HIT_THRESHOLD:
            covered.append(element)
            covered_weight += weight
        else:
            gaps.append(element)

    coverage = Coverage(covered=covered, gaps=gaps, ratio=(covered_weight / total_weight) if total_weight else 0.0)
    return candidates, coverage


def explain_candidates(clue: HotspotClue, candidates: list) -> None:
    """兜底解释（规则版）：给每个候选补上「为什么相关 / 怎么用」。

    输入：线索 + 中间态候选（`MatchCandidate.draft`）；输出：原地写入 `reasons` / `usage`
    并 `finalize()` 成契约终态。解释文案来自 `offline` 规则引擎，P2 会换成模型解释（S2.7）。
    """
    from . import offline

    for candidate in candidates:
        if candidate.reasons:
            candidate.finalize()
            continue
        payload = offline.material_explain(clue.to_dict(), candidate.material.to_dict(),
                                          [h.to_dict() for h in candidate.hits])
        candidate.reasons = payload["reasons"]
        candidate.usage = payload["usage"]
        candidate.finalize()


def dedupe_by_path(materials: list) -> list:
    """同一素材被重复扫描时只保留一条（按绝对路径小写去重，保持首次出现的顺序）。"""
    seen, out = set(), []
    for material in materials:
        key = os.path.abspath(material.path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(material)
    return out

"""热点线索 <-> 素材 的相关性打分。

这一段完全不用大模型：先用确定性算法把候选排出来，再让 Agent 解释和取舍。
好处是结果可复现、可测试，模型只在需要「判断」的位置介入。
"""

from __future__ import annotations

import os
import time

from ..schemas import (DEFAULT_TYPE_WEIGHTS, Coverage, Element, ElementHit,
                       HotspotClue, MatchCandidate, Material)
from ..util import normalize_text, text_similarity

HIT_THRESHOLD = 0.5
FRESH_DAYS = 30
STALE_DAYS = 365


def material_text(material: Material) -> str:
    """素材的可检索文本：标签 + 标题 + 描述。"""
    parts = list(material.tags or []) + [material.title or "", material.description or ""]
    return " ".join(p for p in parts if p)


def score_material(clue: HotspotClue, material: Material, type_weights: dict | None = None) -> tuple:
    """返回 (score, hits, missing)。score 已经归一化到 0~1。"""
    weights = dict(DEFAULT_TYPE_WEIGHTS)
    weights.update(type_weights or {})
    corpus = material_text(material)
    material_elements = material.elements or []

    total_weight = 0.0
    gained = 0.0
    hits: list = []
    missing: list = []

    for element in clue.elements:
        type_weight = weights.get(element.type, 0.5)
        weight = element.score_weight * type_weight
        total_weight += weight

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
        gained += contribution
        if best_sim >= HIT_THRESHOLD:
            hits.append(ElementHit(element_type=element.type, clue_value=element.value,
                                   hit_value=best_value, similarity=best_sim, contribution=contribution))
        else:
            missing.append(element)

    relevance = gained / total_weight if total_weight else 0.0
    score = relevance * (0.9 + 0.1 * material.quality_score)
    score += _recency_bonus(material)
    return max(0.0, min(1.0, score)), hits, missing


def _recency_bonus(material: Material) -> float:
    if not material.mtime:
        return 0.0
    days = (time.time() - material.mtime) / 86400
    if days <= FRESH_DAYS:
        return 0.02
    if days >= STALE_DAYS:
        return -0.03
    return 0.0


def rank_materials(clue: HotspotClue, materials: list, topk: int = 5, min_score: float = 0.0,
                   type_weights: dict | None = None) -> tuple:
    """给所有素材打分排序，返回 (候选列表, 覆盖度)。"""
    scored = []
    coverage_map: dict = {}

    for material in materials:
        score, hits, missing = score_material(clue, material, type_weights)
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
        candidates.append(MatchCandidate(
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
    """兜底解释（规则版）：给每个候选补上「为什么相关 / 怎么用」。"""
    from . import offline

    for candidate in candidates:
        if candidate.reasons:
            continue
        payload = offline.material_explain(clue.to_dict(), candidate.material.to_dict(),
                                          [h.to_dict() for h in candidate.hits])
        candidate.reasons = payload["reasons"]
        candidate.usage = payload["usage"]


def dedupe_by_path(materials: list) -> list:
    """同一素材被重复扫描时只保留一条。"""
    seen, out = set(), []
    for material in materials:
        key = os.path.abspath(material.path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(material)
    return out
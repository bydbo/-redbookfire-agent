"""热点线索 <-> 素材 的要素级匹配与规则解释。

用途：只做确定性判断——把线索要素逐条对到素材上（类型对齐 + 文案/关键词兜底），给出加权
      相关度、命中/缺口，以及规则版解释；**排序与终分在 `tools/retrieval.py`**。
输入：`HotspotClue` + `Material`，以及可选的要素类型权重与**时间基准** `now`。
输出：`ElementMatch` 列表 / `(relevance, hits, missing)` / 时效加减分 / 原地补齐候选的
      `reasons` 与 `usage`。

输出稳定性：时效加减分依赖当前时间，因此时间基准可以用 `now` 注入；
不注入时取 `time.time()`（默认行为不变）。同一输入 + 同一 `now` 必须得到同一结果。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..schemas import (
    DEFAULT_TYPE_WEIGHTS,
    TYPE_LABELS,
    Element,
    ElementHit,
    HotspotClue,
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


def _rule_explain(clue: HotspotClue, material: Material, hits: list) -> dict:
    """规则版匹配解释：由真实 hits 推出「为什么相关 / 怎么用」（确定性，不调模型）。

    文案口径自 S2.7 起内联在这里（此前属已删除的 `offline` 模块），逐字未变：
    `duration_s` 取 `to_dict()` 的两位小数。
    """
    reasons = [f"命中{TYPE_LABELS.get(str(hit.element_type), str(hit.element_type))}："
               f"{hit.clue_value} ↔ 素材的「{hit.hit_value}」"
               for hit in hits[:3]]
    if not reasons:
        reasons.append("没有明显命中，只是兜底候选")

    topic = "这个热点"
    for element in clue.elements or []:
        if element.type == "topic":
            topic = element.value
            break
    usage = f"可用作{topic}的实拍素材，建议放在开头 3 秒或作为过程画面"
    if material.duration_s:
        usage += f"（时长 {round(material.duration_s, 2)} 秒）"
    return {"reasons": reasons, "usage": usage}


def explain_candidates(clue: HotspotClue, candidates: list) -> None:
    """规则版解释：给每个候选补上「为什么相关 / 怎么用」。

    输入：线索 + 中间态候选（`MatchCandidate.draft`）；输出：原地写入 `reasons` / `usage`
    并 `finalize()` 成契约终态。文案由真实 hits 推出，是确定性结果、不调模型；
    E3 的匹配解释 agent 会替换文案来源（那是能力升级，不是降级）。
    """
    for candidate in candidates:
        if candidate.reasons:
            candidate.finalize()
            continue
        payload = _rule_explain(clue, candidate.material, list(candidate.hits))
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

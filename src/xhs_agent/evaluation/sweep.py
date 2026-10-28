"""E9 开发集参数网格与选参规则（纯函数）。

网格只覆盖协议允许的 pg_trgm 阈值和向量距离；选参先过滤误推荐，再按 nDCG / Hit@5 /
Top-1、误推荐、离生产参数距离和字典序破同分。算法不读取留出集。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import Strategy

TRGM_THRESHOLDS = (0.2, 0.3, 0.5)
VECTOR_DISTANCES = (0.45, 0.60, 0.75)
PRODUCTION_SIMILARITY = 0.3
PRODUCTION_DISTANCE = 0.6
FALSE_RECOMMENDATION_MAX = 0.25


@dataclass(frozen=True)
class GridPoint:
    strategy: Strategy
    similarity: float
    distance: float
    label: str


def grid_points() -> list[GridPoint]:
    points: list[GridPoint] = []
    for similarity in TRGM_THRESHOLDS:
        points.append(GridPoint("trgm_only", similarity, PRODUCTION_DISTANCE,
                                f"trgm-s{similarity:.1f}"))
    for distance in VECTOR_DISTANCES:
        points.append(GridPoint("vector_only", PRODUCTION_SIMILARITY, distance,
                                f"vector-d{distance:.2f}"))
    for similarity in TRGM_THRESHOLDS:
        for distance in VECTOR_DISTANCES:
            points.append(GridPoint("hybrid", similarity, distance,
                                    f"hybrid-s{similarity:.1f}-d{distance:.2f}"))
    return points


def _value(metrics: Mapping[str, Any], key: str) -> float:
    value = metrics.get(key)
    return float(value) if value is not None else -1.0


def _point_key(point: GridPoint, metrics: Mapping[str, Any]) -> tuple[Any, ...]:
    distance = abs(point.similarity - PRODUCTION_SIMILARITY)
    distance += abs(point.distance - PRODUCTION_DISTANCE)
    return (
        -_value(metrics, "ndcg"),
        -_value(metrics, "hit5"),
        -_value(metrics, "top1"),
        _value(metrics, "false_recommendation_rate"),
        round(distance, 6),
        point.label,
    )


def choose_point(points: Sequence[GridPoint],
                 metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """按开发集指标选唯一参数点；没有点满足误推荐门槛时明确标不达标。"""
    rows = [(point, metrics.get(point.label, {})) for point in points]
    eligible = [(point, row) for point, row in rows
                if row.get("false_recommendation_rate") is not None
                and float(row["false_recommendation_rate"]) <= FALSE_RECOMMENDATION_MAX]
    pool = eligible or rows
    if not pool:
        raise ValueError("没有可选的参数点")
    point, row = min(pool, key=lambda item: _point_key(item[0], item[1]))
    return {
        "point": point,
        "metrics": dict(row),
        "meets_false_recommendation": bool(eligible),
        "status": "pass" if eligible else "not_meeting_false_recommendation",
    }


def choose_baseline(selected: Mapping[str, GridPoint],
                    metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """在 keyword、已选 trgm、已选 vector 中按同一规则选出简单基线冠军。"""
    candidates: list[tuple[str, GridPoint | None, Mapping[str, Any]]] = [
        ("keyword", None, metrics.get("keyword", {}))]
    for strategy in ("trgm_only", "vector_only"):
        point = selected.get(strategy)
        if point is not None:
            candidates.append((strategy, point, metrics.get(point.label, {})))
    pool = [item for item in candidates if item[2].get("false_recommendation_rate")
            is not None and float(item[2]["false_recommendation_rate"])
            <= FALSE_RECOMMENDATION_MAX] or candidates
    name, point, row = min(
        pool, key=lambda item: _point_key(
            item[1] or GridPoint("keyword", PRODUCTION_SIMILARITY, PRODUCTION_DISTANCE,
                                 "keyword"), item[2]))
    return {"strategy": name, "point": point, "metrics": dict(row)}

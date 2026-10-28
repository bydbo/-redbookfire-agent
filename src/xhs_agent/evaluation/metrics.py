"""E9 指标：Top-1、Hit@5、nDCG@5、空结果、延迟与配对 bootstrap。

口径（协议 §3.4 / §3.5）：
- 三次重复先在任务内平均，再做任务宏平均；重复不增加独立任务数；
- 有答案任务在拆解 / 检索失败或超时时，三项质量指标记 0；
- 无答案任务的失败不计为正确空结果，误推荐率只用技术成功的尝试作分母；
- 延迟采用 nearest-rank，同时报样本、超时和失败数；
- 未执行任务不塞进主表，单独计入 planned / completed。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import Attempt, Case, Qrel

REPEATS = 3
K = 5


def nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    """nearest-rank 分位数：`ceil(p/100 * n)`，1 起始；空列表返回 None。"""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def nDCG_at_k(ranked: Sequence[str], qrels: Mapping[str, int], *, k: int = K) -> float | None:
    """nDCG@k：增益 `2^grade-1`，折扣 `log2(rank+1)`；全库理想排序由 qrels 计算。"""
    grades = [int(qrels.get(key, 0)) for key in ranked[:k]]
    dcg = sum((2 ** grade - 1) / math.log2(index + 2)
              for index, grade in enumerate(grades) if grade > 0)
    ideal = sorted((int(value) for value in qrels.values() if int(value) > 0), reverse=True)[:k]
    if not ideal:
        return None
    idcg = sum((2 ** grade - 1) / math.log2(index + 2)
               for index, grade in enumerate(ideal))
    return round(dcg / idcg, 6) if idcg else None


def _qrels_by_case(qrels: Iterable[Qrel]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for qrel in qrels:
        out[qrel.case_id][qrel.material_key] = int(qrel.grade)
    return out


def _attempt_score(case: Case, qrels: Mapping[str, int], attempt: Attempt) -> dict[str, Any]:
    completed = attempt.status == "completed"
    if case.expectation.answerable:
        if not completed:
            return {"top1": 0.0, "hit5": 0.0, "ndcg": 0.0,
                    "false_recommendation": None, "correct_empty": None}
        ranked = attempt.ranked_material_keys
        top1 = bool(ranked) and qrels.get(ranked[0], 0) >= 1
        hit5 = any(qrels.get(key, 0) >= 1 for key in ranked[:K])
        return {"top1": float(top1), "hit5": float(hit5),
                "ndcg": nDCG_at_k(ranked, qrels, k=K) or 0.0,
                "false_recommendation": None, "correct_empty": None}
    successful = completed
    ranked = attempt.ranked_material_keys
    return {
        "top1": None,
        "hit5": None,
        "ndcg": None,
        "false_recommendation": bool(ranked) if successful else None,
        "correct_empty": (not ranked) if successful else False,
    }


def case_metrics(case: Case, qrels: Sequence[Qrel], attempts: Sequence[Attempt],
                 *, repeats: int = REPEATS, k: int = K) -> dict[str, Any]:
    """一个任务的三次重复聚合；缺失 repeat 明确标记，不伪装成失败或成功。"""
    by_repeat: dict[int, Attempt] = {}
    for attempt in attempts:
        if attempt.case_id != case.case_id:
            continue
        by_repeat.setdefault(int(attempt.repeat), attempt)
    missing = [index for index in range(1, repeats + 1) if index not in by_repeat]
    ordered = [by_repeat[index] for index in sorted(by_repeat)]
    qrel_map = {qrel.material_key: int(qrel.grade) for qrel in qrels
                if qrel.case_id == case.case_id}
    scored = [_attempt_score(case, qrel_map, attempt) for attempt in ordered]
    completed = sum(1 for attempt in ordered if attempt.status == "completed")
    latencies = [attempt.total_latency_ms for attempt in ordered
                 if attempt.status == "completed" and attempt.total_latency_ms is not None]
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "group_id": case.group_id,
        "split": case.split,
        "input_type": case.input_type,
        "answerable": case.expectation.answerable,
        "attempts": len(ordered),
        "planned": repeats,
        "completed": completed,
        "technical_completion": round(completed / repeats, 6),
        "complete": not missing,
        "missing_repeats": missing,
        "latency_ms": latencies,
        "cost_cny": round(sum(float(attempt.cost_cny) for attempt in ordered), 8),
        "unknown_cost_cny": round(sum(float(attempt.unknown_cost_cny) for attempt in ordered), 8),
    }
    if case.expectation.answerable:
        if scored:
            row["top1"] = round(sum(float(item["top1"] or 0) for item in scored) / len(scored), 6)
            row["hit5"] = round(sum(float(item["hit5"] or 0) for item in scored) / len(scored), 6)
            row["ndcg"] = round(sum(float(item["ndcg"] or 0) for item in scored) / len(scored), 6)
        else:
            row["top1"] = row["hit5"] = row["ndcg"] = 0.0
        row["false_recommendation"] = None
        row["correct_empty"] = None
    else:
        row["top1"] = row["hit5"] = row["ndcg"] = None
        successful = [item for item in scored if item["false_recommendation"] is not None]
        false_count = sum(1 for item in successful if item["false_recommendation"])
        row["false_recommendation"] = (
            round(false_count / len(successful), 6) if successful else None)
        row["successful_attempts"] = len(successful)
        row["false_recommendations"] = false_count
        row["correct_empty"] = round(
            sum(1 for item in scored if item["correct_empty"]) / repeats, 6)
    return row


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def aggregate(cases: Sequence[Case], qrels: Sequence[Qrel], attempts: Sequence[Attempt],
              *, repeats: int = REPEATS, k: int = K) -> dict[str, Any]:
    """把逐任务结果聚合成主表；只对执行过的任务计质量均值，另报计划 / 完成数。"""
    by_case: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in attempts:
        by_case[attempt.case_id].append(attempt)
    rows = [case_metrics(case, qrels, by_case.get(case.case_id, []),
                         repeats=repeats, k=k) for case in cases]
    answerable = [row for row in rows if row["answerable"]]
    empty = [row for row in rows if not row["answerable"]]
    scored_answerable = [row for row in answerable if row["attempts"] > 0]
    completed_latencies = [latency for row in rows for latency in row["latency_ms"]]
    false_numerator = sum(int(row.get("false_recommendations", 0)) for row in empty)
    false_denominator = sum(int(row.get("successful_attempts", 0)) for row in empty)
    correct_empty = sum(float(row["correct_empty"] or 0) * repeats for row in empty)
    planned = len(cases) * repeats
    completed = sum(row["completed"] for row in rows)
    return {
        "cases": len(cases),
        "answerable_cases": len(answerable),
        "unanswerable_cases": len(empty),
        "cases_complete": sum(1 for row in rows if row["complete"]),
        "planned_attempts": planned,
        "completed_attempts": completed,
        "technical_completion": round(completed / planned, 6) if planned else 0.0,
        "top1": _mean([float(row["top1"] or 0) for row in scored_answerable]),
        "hit5": _mean([float(row["hit5"] or 0) for row in scored_answerable]),
        "ndcg": _mean([float(row["ndcg"] or 0) for row in scored_answerable]),
        "false_recommendation_rate": (
            round(false_numerator / false_denominator, 6) if false_denominator else None),
        "correct_empty_rate": round(correct_empty / (len(empty) * repeats), 6) if empty else None,
        "latency_p50_ms": nearest_rank(completed_latencies, 50),
        "latency_p95_ms": nearest_rank(completed_latencies, 95),
        "latency_samples": len(completed_latencies),
        "timeouts": sum(1 for row in rows for attempt in by_case.get(row["case_id"], [])
                        if attempt.status == "timeout"),
        "failures": sum(1 for row in rows for attempt in by_case.get(row["case_id"], [])
                        if attempt.status == "failed"),
        "not_run": sum(1 for row in rows for attempt in by_case.get(row["case_id"], [])
                       if attempt.status == "not_run") + sum(
                           len(row["missing_repeats"]) for row in rows),
        "cost_cny": round(sum(row["cost_cny"] for row in rows), 8),
        "unknown_cost_cny": round(sum(row["unknown_cost_cny"] for row in rows), 8),
        "rows": rows,
    }


def paired_bootstrap(groups: Sequence[str], baseline: Sequence[float],
                     hybrid: Sequence[float], *, samples: int = 10_000,
                     seed: int = 20261027) -> dict[str, Any]:
    """按独立主题 / 来源组重采样的配对 bootstrap；返回差值、95% 区间与 p 值。

    输入已按任务三次重复聚合；同组任务一起进出，避免把同一需求的改写当成独立样本。
    """
    if not (len(groups) == len(baseline) == len(hybrid)):
        raise ValueError("groups / baseline / hybrid 长度必须一致")
    if not groups:
        return {"delta": None, "lower": None, "upper": None, "p_value": None,
                "samples": samples, "groups": 0}
    clustered: dict[str, list[int]] = defaultdict(list)
    for index, group in enumerate(groups):
        clustered[group].append(index)
    keys = list(clustered)
    rng = random.Random(seed)
    deltas: list[float] = []
    observed = sum(h - b for b, h in zip(baseline, hybrid, strict=True)) / len(groups)
    for _ in range(samples):
        chosen = [keys[rng.randrange(len(keys))] for _ in keys]
        base_values: list[float] = []
        hybrid_values: list[float] = []
        for key in chosen:
            for index in clustered[key]:
                base_values.append(float(baseline[index]))
                hybrid_values.append(float(hybrid[index]))
        deltas.append((sum(hybrid_values) - sum(base_values)) / len(base_values))
    ordered = sorted(deltas)
    lower = ordered[max(0, int(0.025 * len(ordered)) - 1)]
    upper = ordered[min(len(ordered) - 1, int(0.975 * len(ordered)))]
    p_value = sum(1 for value in deltas if value <= 0) / len(deltas)
    return {
        "delta": round(observed, 6),
        "lower": round(lower, 6),
        "upper": round(upper, 6),
        "p_value": round(p_value, 6),
        "samples": samples,
        "groups": len(keys),
    }


def case_deltas(cases: Sequence[Case], baseline: Mapping[str, float],
                hybrid: Mapping[str, float]) -> tuple[list[str], list[float], list[float]]:
    """按 case 取 nDCG 差值，供 bootstrap 使用；缺失任务直接报错，不静默丢样本。"""
    groups: list[str] = []
    base: list[float] = []
    new: list[float] = []
    for case in cases:
        if case.case_id not in baseline or case.case_id not in hybrid:
            raise ValueError(f"任务缺少对照结果：{case.case_id}")
        groups.append(case.group_id)
        base.append(float(baseline[case.case_id]))
        new.append(float(hybrid[case.case_id]))
    return groups, base, new

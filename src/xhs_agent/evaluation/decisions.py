"""E9 判定：先证据完整性，再质量门槛，最后才是相对简单基线的收益。

三类判定互不替代：负面结果可以通过 evidence，同时不通过 quality / benefit。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .models import Attempt, Case, Decision, HumanScore, Qrel

TOP1_MIN = 0.50
HIT5_MIN = 0.80
FALSE_RECOMMENDATION_MAX = 0.20
E2E_SUCCESS_MIN = 0.80
CONSTRAINT_MIN = 0.95
NDCG_DELTA_MIN = 0.05


def evidence_gate(cases: Sequence[Case], attempts: Sequence[Attempt],
                  qrels: Sequence[Qrel], *, frozen: bool,
                  repeats: int = 3,
                  human_scores: Sequence[HumanScore] | None = None,
                  require_human: bool = False,
                  expected_e2e_attempts: int | None = None,
                  strategies: Sequence[str] | None = None) -> Decision:
    """证据完整性：数据冻结、尝试齐全、qrel 已确认、需人工评分时无缺项。"""
    reasons: list[str] = []
    missing: list[str] = []
    if not frozen:
        missing.append("数据 manifest 未冻结")
    case_ids = {case.case_id for case in cases}
    by_case: dict[str, list[Attempt]] = defaultdict(list)
    for attempt in attempts:
        if attempt.case_id not in case_ids:
            missing.append(f"尝试引用不存在的任务：{attempt.case_id}")
            continue
        by_case[attempt.case_id].append(attempt)
    terminal = {"completed", "failed", "timeout"}
    for case in cases:
        rows = by_case.get(case.case_id, [])
        repeats_seen = {int(attempt.repeat) for attempt in rows}
        for index in range(1, repeats + 1):
            if index not in repeats_seen:
                missing.append(f"{case.case_id} 缺少 repeat {index}")
            if strategies:
                seen = {attempt.strategy for attempt in rows if attempt.repeat == index}
                for strategy in strategies:
                    if strategy not in seen:
                        missing.append(
                            f"{case.case_id} repeat {index} 缺少策略 {strategy}")
        for attempt in rows:
            if attempt.status not in terminal:
                missing.append(f"{attempt.attempt_id} 未终态：{attempt.status}")
    for qrel in qrels:
        if not qrel.confirmed:
            missing.append(f"qrel 未确认：{qrel.case_id}/{qrel.material_key}")
    if require_human:
        scored_attempts = {score.attempt_id for score in human_scores or []}
        for rows in by_case.values():
            for attempt in rows:
                if attempt.attempt_id not in scored_attempts:
                    missing.append(f"缺少人工评分：{attempt.attempt_id}")
    if expected_e2e_attempts is not None and len(attempts) != expected_e2e_attempts:
        missing.append(f"E2E 尝试数 {len(attempts)} != {expected_e2e_attempts}")
    if not cases:
        missing.append("没有任务")
    if missing:
        reasons.append("证据不完整")
    return Decision(gate="evidence", status="incomplete" if missing else "pass",
                    reasons=reasons, missing=missing,
                    metrics={"cases": len(cases), "attempts": len(attempts)})


def quality_gate(metrics: Mapping[str, Any]) -> Decision:
    """留出真实拆解的质量门槛；缺失指标视为未完成，不当作通过。"""
    reasons: list[str] = []
    missing: list[str] = []
    required = ("top1", "hit5", "false_recommendation_rate", "technical_completion",
                "unanswerable_cases")
    for key in required:
        if metrics.get(key) is None:
            missing.append(f"缺少指标：{key}")
    if missing:
        return Decision(gate="quality", status="incomplete", reasons=["指标不齐"],
                        missing=missing, metrics=dict(metrics))
    top1 = float(metrics["top1"])
    hit5 = float(metrics["hit5"])
    false_rate = float(metrics["false_recommendation_rate"])
    technical = float(metrics["technical_completion"])
    unanswerable_cases = int(metrics["unanswerable_cases"])
    if top1 < TOP1_MIN:
        reasons.append(f"Top-1 {top1:.1%} < {TOP1_MIN:.0%}")
    if hit5 < HIT5_MIN:
        reasons.append(f"Hit@5 {hit5:.1%} < {HIT5_MIN:.0%}")
    if false_rate > FALSE_RECOMMENDATION_MAX:
        reasons.append(f"无答案误推荐率 {false_rate:.1%} > {FALSE_RECOMMENDATION_MAX:.0%}")
    if technical < 1.0:
        reasons.append(f"技术完成率 {technical:.1%} < 100%")
    if unanswerable_cases <= 0:
        reasons.append("没有无答案任务，不能判定空结果门槛")
    if metrics.get("quality_violations"):
        reasons.append(f"出现 {metrics['quality_violations']} 次禁止性错误")
    return Decision(gate="quality", status="fail" if reasons else "pass",
                    reasons=reasons, missing=[], metrics=dict(metrics))


def benefit_gate(hybrid: Mapping[str, Any], baseline: Mapping[str, Any],
                 bootstrap: Mapping[str, Any]) -> Decision:
    """证明混合策略有收益：ΔnDCG、配对 CI 下界、Top-1/Hit@5/误推荐不下降。"""
    reasons: list[str] = []
    missing: list[str] = []
    for label, values in (("hybrid", hybrid), ("baseline", baseline)):
        for key in ("top1", "hit5", "false_recommendation_rate"):
            if values.get(key) is None:
                missing.append(f"{label} 缺少 {key}")
    if bootstrap.get("delta") is None or bootstrap.get("lower") is None:
        missing.append("缺少配对 bootstrap 结果")
    if missing:
        return Decision(gate="benefit", status="incomplete", reasons=["收益证据不齐"],
                        missing=missing, metrics={"bootstrap": dict(bootstrap)})
    delta = float(bootstrap["delta"])
    lower = float(bootstrap["lower"])
    if delta < NDCG_DELTA_MIN:
        reasons.append(f"ΔnDCG@5 {delta:.3f} < {NDCG_DELTA_MIN}")
    if lower <= 0:
        reasons.append(f"bootstrap 95% 区间下界 {lower:.3f} <= 0")
    if float(hybrid["top1"]) < float(baseline["top1"]):
        reasons.append("Top-1 下降")
    if float(hybrid["hit5"]) < float(baseline["hit5"]):
        reasons.append("Hit@5 下降")
    if float(hybrid["false_recommendation_rate"]) > float(
            baseline["false_recommendation_rate"]):
        reasons.append("无答案误推荐率上升")
    return Decision(gate="benefit", status="fail" if reasons else "pass",
                    reasons=reasons, missing=[], metrics={
                        "hybrid": dict(hybrid), "baseline": dict(baseline),
                        "bootstrap": dict(bootstrap)})

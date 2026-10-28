"""E9 指标：nDCG、重复聚合、无答案、nearest-rank 与配对 bootstrap。"""

from __future__ import annotations

import math

import pytest

from xhs_agent.evaluation.metrics import (
    aggregate,
    case_metrics,
    nDCG_at_k,
    nearest_rank,
    paired_bootstrap,
)
from xhs_agent.evaluation.models import Attempt, Case, Qrel


def _case(case_id: str = "case-a", *, answerable: bool = True, split: str = "dev",
          group: str = "g1", input_type: str = "text") -> Case:
    return Case.model_validate({
        "case_id": case_id, "group_id": group, "split": split, "input_type": input_type,
        "natural_input": {"text": "测试"}, "expectation": {"answerable": answerable,
        "summary": "测试"}, "oracle": {"elements": [{"type": "topic", "value": "早餐"}]},
        "scenario_tags": ["natural_naming"], "notes": "",
    })


def _qrels(*rows: tuple[str, str, int]) -> list[Qrel]:
    return [Qrel(case_id=case_id, material_key=material, grade=grade, reason="r",
                 reviewer="t", confirmed=True) for case_id, material, grade in rows]


def _attempt(case_id: str, repeat: int, ranked: list[str], *,
             status: str = "completed") -> Attempt:
    return Attempt(attempt_id=f"{case_id}-{repeat}", case_id=case_id, repeat=repeat,
                   strategy="hybrid", clue_mode="oracle", status=status,  # type: ignore[arg-type]
                   ranked_material_keys=ranked, total_latency_ms=100)


def test_ndcg_hand_computed() -> None:
    qrels = {"a": 2, "b": 1, "c": 0}
    value = nDCG_at_k(["b", "a"], qrels, k=5)
    dcg = 1 / math.log2(2) + 3 / math.log2(3)
    idcg = 3 / math.log2(2) + 1 / math.log2(3)
    assert value == pytest.approx(dcg / idcg, abs=1e-6)


def test_ndcg_is_none_without_relevant_material() -> None:
    assert nDCG_at_k(["a"], {"a": 0}, k=5) is None


def test_case_failure_counts_as_zero_for_answerable() -> None:
    case = _case(answerable=True)
    attempts = [_attempt(case.case_id, index, [], status="failed") for index in (1, 2, 3)]
    row = case_metrics(case, _qrels((case.case_id, "a", 2)), attempts)
    assert row["top1"] == 0.0
    assert row["hit5"] == 0.0
    assert row["ndcg"] == 0.0
    assert row["complete"] is True


def test_case_repeats_are_averaged_before_macro() -> None:
    case = _case(answerable=True)
    attempts = [
        _attempt(case.case_id, 1, ["a"]),
        _attempt(case.case_id, 2, ["b"]),
        _attempt(case.case_id, 3, ["a"]),
    ]
    row = case_metrics(case, _qrels((case.case_id, "a", 2)), attempts)
    assert row["top1"] == pytest.approx(2 / 3, abs=1e-6)
    assert row["hit5"] == pytest.approx(2 / 3, abs=1e-6)


def test_unanswerable_failure_is_not_correct_empty() -> None:
    case = _case("case-empty", answerable=False, split="holdout")
    attempts = [_attempt(case.case_id, index, [], status="failed") for index in (1, 2, 3)]
    row = case_metrics(case, [], attempts)
    assert row["correct_empty"] == 0.0
    assert row["false_recommendation"] is None
    assert row["technical_completion"] == 0.0


def test_unanswerable_false_recommendation_rate_uses_successful_attempts() -> None:
    case = _case("case-empty", answerable=False, split="holdout")
    attempts = [
        _attempt(case.case_id, 1, ["a"]),
        _attempt(case.case_id, 2, [], status="failed"),
        _attempt(case.case_id, 3, []),
    ]
    row = case_metrics(case, [], attempts)
    assert row["false_recommendation"] == pytest.approx(0.5)
    assert row["correct_empty"] == pytest.approx(1 / 3)
    assert row["technical_completion"] == pytest.approx(2 / 3)


def test_nearest_rank_uses_ceiling() -> None:
    assert nearest_rank([4, 1, 3, 2], 50) == 2
    assert nearest_rank([4, 1, 3, 2], 95) == 4
    assert nearest_rank([], 95) is None


def test_aggregate_macro_and_empty_rates() -> None:
    answer = _case("case-a", answerable=True)
    empty = _case("case-empty", answerable=False, split="holdout")
    cases = [answer, empty]
    attempts = [
        _attempt(answer.case_id, 1, ["a"]),
        _attempt(answer.case_id, 2, ["a"]),
        _attempt(answer.case_id, 3, ["a"]),
        _attempt(empty.case_id, 1, []),
        _attempt(empty.case_id, 2, ["a"]),
        _attempt(empty.case_id, 3, []),
    ]
    result = aggregate(cases, _qrels((answer.case_id, "a", 2)), attempts)
    assert result["top1"] == pytest.approx(1.0)
    assert result["hit5"] == pytest.approx(1.0)
    assert result["false_recommendation_rate"] == pytest.approx(1 / 3)
    assert result["correct_empty_rate"] == pytest.approx(2 / 3)
    assert result["planned_attempts"] == 6
    assert result["completed_attempts"] == 6


def test_paired_bootstrap_is_deterministic_and_positive() -> None:
    groups = [f"g{index}" for index in range(8)]
    baseline = [0.2] * 8
    hybrid = [0.4] * 8
    first = paired_bootstrap(groups, baseline, hybrid, samples=500, seed=20261027)
    second = paired_bootstrap(groups, baseline, hybrid, samples=500, seed=20261027)
    assert first == second
    assert first["delta"] == pytest.approx(0.2)
    assert first["lower"] > 0


def test_case_requires_all_three_repeats_to_be_complete() -> None:
    case = _case(answerable=True)
    row = case_metrics(case, _qrels((case.case_id, "a", 2)),
                       [_attempt(case.case_id, 1, ["a"])])
    assert row["complete"] is False
    assert row["missing_repeats"] == [2, 3]

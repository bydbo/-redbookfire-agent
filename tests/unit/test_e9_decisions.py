"""E9 三类判定：证据完整性、质量门槛与相对简单基线的收益。"""

from __future__ import annotations

from xhs_agent.evaluation.decisions import benefit_gate, evidence_gate, quality_gate
from xhs_agent.evaluation.models import Attempt, Case, Qrel


def _case(case_id: str, *, answerable: bool = True, split: str = "holdout",
          input_type: str = "text", group: str = "g1") -> Case:
    return Case.model_validate({
        "case_id": case_id, "group_id": group, "split": split, "input_type": input_type,
        "natural_input": {"text": "测试"}, "expectation": {"answerable": answerable,
        "summary": "测试"}, "oracle": {"elements": [{"type": "topic", "value": "早餐"}]},
        "scenario_tags": ["natural_naming"], "notes": "",
    })


def _attempt(case_id: str, repeat: int) -> Attempt:
    return Attempt(attempt_id=f"{case_id}-{repeat}", case_id=case_id, repeat=repeat,
                   strategy="hybrid", clue_mode="real", status="completed",
                   ranked_material_keys=["a"] if repeat != 2 else [])


def _qrels(case_id: str) -> list[Qrel]:
    return [Qrel(case_id=case_id, material_key="a", grade=2, reason="r",
                 reviewer="t", confirmed=True)]


def test_evidence_gate_requires_all_repeats() -> None:
    cases = [_case("case-a")]
    attempts = [_attempt("case-a", 1), _attempt("case-a", 2)]
    decision = evidence_gate(cases, attempts, _qrels("case-a"), frozen=True)
    assert decision.status == "incomplete"
    assert any("case-a" in item for item in decision.missing)


def test_evidence_gate_passes_complete_terminal_attempts() -> None:
    cases = [_case("case-a")]
    attempts = [_attempt("case-a", index) for index in (1, 2, 3)]
    decision = evidence_gate(cases, attempts, _qrels("case-a"), frozen=True)
    assert decision.status == "pass", decision.reasons


def test_evidence_gate_rejects_draft() -> None:
    cases = [_case("case-a")]
    attempts = [_attempt("case-a", index) for index in (1, 2, 3)]
    decision = evidence_gate(cases, attempts, _qrels("case-a"), frozen=False)
    assert decision.status == "incomplete"


def test_quality_gate_pass_on_thresholds() -> None:
    metrics = {"top1": 0.55, "hit5": 0.85, "false_recommendation_rate": 0.1,
               "technical_completion": 1.0, "unanswerable_cases": 6}
    decision = quality_gate(metrics)
    assert decision.status == "pass", decision.reasons


def test_quality_gate_fails_below_top1() -> None:
    metrics = {"top1": 0.49, "hit5": 0.9, "false_recommendation_rate": 0.1,
               "technical_completion": 1.0, "unanswerable_cases": 6}
    decision = quality_gate(metrics)
    assert decision.status == "fail"
    assert any("Top-1" in reason for reason in decision.reasons)


def test_quality_gate_requires_unanswerable_attempts_complete() -> None:
    metrics = {"top1": 0.6, "hit5": 0.9, "false_recommendation_rate": 0.1,
               "technical_completion": 0.99, "unanswerable_cases": 6}
    decision = quality_gate(metrics)
    assert decision.status == "fail"


def test_benefit_gate_requires_delta_and_ci() -> None:
    base = {"top1": 0.5, "hit5": 0.8, "false_recommendation_rate": 0.1}
    hybrid = {"top1": 0.55, "hit5": 0.85, "false_recommendation_rate": 0.1}
    decision = benefit_gate(hybrid, base,
                            {"delta": 0.06, "lower": 0.01, "upper": 0.12})
    assert decision.status == "pass", decision.reasons


def test_benefit_gate_fails_when_top1_drops() -> None:
    base = {"top1": 0.6, "hit5": 0.8, "false_recommendation_rate": 0.1}
    hybrid = {"top1": 0.55, "hit5": 0.86, "false_recommendation_rate": 0.1}
    decision = benefit_gate(hybrid, base,
                            {"delta": 0.06, "lower": 0.01, "upper": 0.12})
    assert decision.status == "fail"


def test_benefit_gate_fails_when_ci_crosses_zero() -> None:
    base = {"top1": 0.5, "hit5": 0.8, "false_recommendation_rate": 0.1}
    hybrid = {"top1": 0.55, "hit5": 0.85, "false_recommendation_rate": 0.1}
    decision = benefit_gate(hybrid, base,
                            {"delta": 0.06, "lower": -0.01, "upper": 0.12})
    assert decision.status == "fail"


def test_benefit_gate_fails_when_false_recommendation_increases() -> None:
    base = {"top1": 0.5, "hit5": 0.8, "false_recommendation_rate": 0.1}
    hybrid = {"top1": 0.55, "hit5": 0.85, "false_recommendation_rate": 0.15}
    decision = benefit_gate(hybrid, base,
                            {"delta": 0.06, "lower": 0.01, "upper": 0.12})
    assert decision.status == "fail"

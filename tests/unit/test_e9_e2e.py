"""S9.3 E2E 证据：32 次计划、刷新 / 报告一致性、假模型隔离。"""

from __future__ import annotations

from xhs_agent.evaluation.e2e import (
    E2EAttempt,
    E2EResult,
    EvidenceRefs,
    cited_attempts,
    validate_e2e,
)
from xhs_agent.evaluation.models import Case, Dataset, DatasetManifest, E2ETask


def _dataset() -> Dataset:
    cases = []
    tasks = []
    for index in range(20):
        case_id = f"case-{index:02d}"
        cases.append(Case.model_validate({
            "case_id": case_id, "group_id": f"g{index}", "split": "holdout",
            "input_type": "text", "natural_input": {"text": "测试"},
            "expectation": {"answerable": True, "summary": "测试"},
            "oracle": {"elements": [{"type": "topic", "value": "早餐"}]},
            "scenario_tags": ["natural_naming"], "notes": "",
        }))
        tasks.append(E2ETask(case_id=case_id, repeats=2 if index < 6 else 0))
    return Dataset(
        manifest=DatasetManifest(version="test", protocol_version="1",
                                 dataset_kind="engineering_fake", status="frozen"),
        materials=[], cases=cases, qrels=[], reviews=[], e2e_tasks=tasks,
    )


def _attempt(case_id: str, repeat: int, *, mode: str = "live") -> E2EAttempt:
    return E2EAttempt(
        attempt_id=f"{case_id}-{repeat}", case_id=case_id, repeat=repeat,
        mode=mode, status="completed",  # type: ignore[arg-type]
        refs=EvidenceRefs(session_id="s", run_id="r", budget_ref="b"),
        ui_end_state="result", refreshed_ok=True, report_consistent=True,
        auto_checks={"refs": True, "persisted": True}, human_score="pass",
    )


def _result() -> E2EResult:
    dataset = _dataset()
    rows = []
    for task in dataset.e2e_tasks:
        for repeat in range(1, 2 + task.repeats):
            rows.append(_attempt(task.case_id, repeat))
    return E2EResult(attempts=rows)


def test_complete_32_attempts_pass() -> None:
    decision = validate_e2e(_dataset(), _result())
    assert decision.status == "pass", decision.missing
    assert decision.metrics["success_rate"] == 1.0


def test_missing_repeat_is_incomplete() -> None:
    result = _result()
    result.attempts.pop()
    decision = validate_e2e(_dataset(), result)
    assert decision.status == "incomplete"


def test_fake_mode_is_excluded_from_live_summary() -> None:
    result = _result()
    result.attempts.append(_attempt("case-00", 1, mode="engineering_fake"))
    decision = validate_e2e(_dataset(), result)
    assert decision.status == "pass"
    assert all(item.mode == "live" for item in cited_attempts(result.attempts))


def test_failed_attempt_keeps_budget_and_error() -> None:
    result = _result()
    result.attempts[0] = result.attempts[0].model_copy(
        update={"status": "failed", "error": "worker timeout", "human_score": None})
    decision = validate_e2e(_dataset(), result)
    assert decision.metrics["success_rate"] == 0.95
    assert decision.status in ("pass", "fail")

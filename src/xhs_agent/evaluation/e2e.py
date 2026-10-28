"""S9.3 真实端到端证据：20 个任务 / 32 次尝试的结构化记录与校验。

本模块只校验证据，不启动浏览器；`engineering_fake` 结果永远不能计入真实效果汇总。
一次 live 尝试必须保留 session / run / 结果引用、预算引用、自动检查、刷新与报告一致性。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Dataset

Mode = Literal["live", "engineering_fake"]
Verdict = Literal["pass", "fail", "uncertain"]


class E2EModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceRefs(E2EModel):
    session_id: str = ""
    message_id: str = ""
    turn_id: str = ""
    job_id: str = ""
    run_id: str = ""
    artifact_version: str = ""
    report_ref: str = ""
    budget_ref: str = ""


class E2EAttempt(E2EModel):
    attempt_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    repeat: int = Field(ge=1, le=3)
    mode: Mode
    status: Literal["completed", "failed", "timeout"]
    input_summary: str = ""
    refs: EvidenceRefs = Field(default_factory=EvidenceRefs)
    ui_end_state: str = ""
    refreshed_ok: bool = False
    report_consistent: bool = False
    auto_checks: dict[str, bool] = Field(default_factory=dict)
    human_score: Verdict | None = None
    error: str = ""
    cost_cny: float = 0.0
    unknown_cost_cny: float = 0.0


class E2EResult(E2EModel):
    attempts: list[E2EAttempt] = Field(default_factory=list)


class E2EDecision(E2EModel):
    status: Literal["pass", "fail", "incomplete"]
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


def planned_attempts(dataset: Dataset) -> dict[str, int]:
    """由 `e2e.json` 推出每个 case 的计划尝试数：首次 + 额外重复。"""
    return {task.case_id: 1 + task.repeats for task in dataset.e2e_tasks}


def validate_e2e(dataset: Dataset, result: E2EResult) -> E2EDecision:
    """校验 20 个任务 / 32 次 live 尝试；假模型结果只留在工程诊断。"""
    planned = planned_attempts(dataset)
    missing: list[str] = []
    reasons: list[str] = []
    by_case: dict[str, list[E2EAttempt]] = defaultdict(list)
    for attempt in result.attempts:
        if attempt.mode != "live":
            continue
        if attempt.case_id not in planned:
            missing.append(f"E2E 尝试引用未登记任务：{attempt.case_id}")
            continue
        by_case[attempt.case_id].append(attempt)
    for case_id, expected in planned.items():
        rows = sorted(by_case.get(case_id, []), key=lambda item: item.repeat)
        repeats = {row.repeat for row in rows}
        for index in range(1, expected + 1):
            if index not in repeats:
                missing.append(f"{case_id} 缺少 repeat {index}")
        if len(rows) != expected:
            missing.append(f"{case_id} 尝试数 {len(rows)} != {expected}")
        for row in rows:
            if not row.refs.budget_ref:
                missing.append(f"{row.attempt_id} 缺少预算引用")
            if row.status == "completed":
                for field in ("session_id", "run_id"):
                    if not getattr(row.refs, field):
                        missing.append(f"{row.attempt_id} 缺少 {field}")
                if not row.refreshed_ok:
                    missing.append(f"{row.attempt_id} 未记录刷新恢复")
                if not row.report_consistent:
                    missing.append(f"{row.attempt_id} 未记录报告一致性")
                if not row.auto_checks:
                    missing.append(f"{row.attempt_id} 缺少自动检查")
                if row.human_score is None:
                    missing.append(f"{row.attempt_id} 缺少人工评分")
            elif not row.error:
                missing.append(f"{row.attempt_id} 失败但没有错误记录")
    if not planned:
        missing.append("e2e.json 没有可校验的任务")
    if len(planned) != 20:
        missing.append(f"E2E 唯一任务 {len(planned)} != 20")
    if sum(planned.values()) != 32:
        missing.append(f"E2E 计划尝试 {sum(planned.values())} != 32")
    first_attempts = [sorted(rows, key=lambda item: item.repeat)[0]
                      for rows in by_case.values() if rows]
    success = sum(1 for row in first_attempts
                  if row.status == "completed" and row.human_score == "pass"
                  and all(row.auto_checks.values()) and row.refreshed_ok
                  and row.report_consistent)
    checks = [value for row in first_attempts for value in row.auto_checks.values()]
    constraint = (sum(1 for value in checks if value) / len(checks)) if checks else None
    success_rate = round(success / 20, 6)
    if missing:
        reasons.append("证据不完整")
    elif success_rate < 0.8:
        reasons.append(f"端到端成功率 {success_rate:.1%} < 80%")
    if constraint is not None and constraint < 0.95:
        reasons.append(f"约束遵守率 {constraint:.1%} < 95%")
    if not missing and not reasons:
        status: Literal["pass", "fail", "incomplete"] = "pass"
    elif missing:
        status = "incomplete"
    else:
        status = "fail"
    return E2EDecision(status=status, reasons=reasons, missing=missing,
                       metrics={"success_rate": success_rate,
                                "constraint_compliance": constraint,
                                "live_attempts": sum(len(rows) for rows in by_case.values()),
                                "planned_attempts": sum(planned.values())})


def cited_attempts(attempts: Sequence[E2EAttempt]) -> list[E2EAttempt]:
    """只返回 live 尝试，供报告汇总；engineering_fake 永不进入真实效果表。"""
    return [attempt for attempt in attempts if attempt.mode == "live"]

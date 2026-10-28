"""S9.1 盲标表：任务 × 素材全矩阵导出与回填导入。

导出给人工逐格打 0/1/2，导入后生成 qrels 与每任务全库审阅记录。完整矩阵是刻意设计：
稀疏正例容易把“没看全库”误当成“严格无答案”，5,000 格必须逐格确认。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from .models import CaseReview, Dataset, Qrel

HEADER = ("case_id", "material_key", "grade", "reason", "reviewer", "confirmed")
GRADE_TEXT = {"0", "1", "2"}


def export_rows(dataset: Dataset) -> list[dict[str, str]]:
    """按 case_id / material_key 排序生成完整矩阵；grade 留空待人工填写。"""
    rows: list[dict[str, str]] = []
    for case in sorted(dataset.cases, key=lambda item: item.case_id):
        for material in sorted(dataset.materials, key=lambda item: item.material_key):
            rows.append({"case_id": case.case_id, "material_key": material.material_key,
                         "grade": "", "reason": "", "reviewer": "", "confirmed": "false"})
    return rows


def write_csv(path: Path, rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(HEADER))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != HEADER:
            raise ValueError(f"CSV 表头必须是 {HEADER}，实际 {tuple(reader.fieldnames or ())}")
        return [dict(row) for row in reader]


def _bool(value: str) -> bool:
    text = value.strip().lower()
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n", ""):
        return False
    raise ValueError(f"confirmed 只能是 true/false：{value!r}")


def build_qrels(dataset: Dataset, rows: list[dict[str, str]], *,
                reviewer: str = "") -> tuple[list[Qrel], list[CaseReview]]:
    """校验完整矩阵并生成 qrels / 审阅记录；缺格、重复、非法等级都失败。"""
    expected = {(case.case_id, material.material_key)
                for case in dataset.cases for material in dataset.materials}
    seen: set[tuple[str, str]] = set()
    qrels: list[Qrel] = []
    for index, row in enumerate(rows, start=2):
        case_id = (row.get("case_id") or "").strip()
        material_key = (row.get("material_key") or "").strip()
        key = (case_id, material_key)
        if key not in expected:
            raise ValueError(f"第 {index} 行引用未知任务 / 素材：{case_id} / {material_key}")
        if key in seen:
            raise ValueError(f"第 {index} 行重复：{case_id} / {material_key}")
        seen.add(key)
        grade_text = (row.get("grade") or "").strip()
        if grade_text not in GRADE_TEXT:
            raise ValueError(f"第 {index} 行 grade 必须是 0/1/2：{grade_text!r}")
        grade = int(grade_text)
        reason = (row.get("reason") or "").strip()
        if grade >= 1 and not reason:
            raise ValueError(f"第 {index} 行 grade={grade} 必须写理由")
        row_reviewer = (row.get("reviewer") or "").strip() or reviewer.strip()
        if not row_reviewer:
            raise ValueError(f"第 {index} 行缺少 reviewer")
        qrels.append(Qrel(case_id=case_id, material_key=material_key, grade=grade,
                          reason=reason or "无关", reviewer=row_reviewer,
                          confirmed=_bool(row.get("confirmed") or "")))
    missing = sorted(expected - seen)
    if missing:
        preview = ", ".join(f"{case}/{material}" for case, material in missing[:5])
        raise ValueError(f"盲标矩阵缺 {len(missing)} 格：{preview}")
    reviews = [CaseReview(case_id=case.case_id,
                          reviewed_materials=len(dataset.materials),
                          library_size=len(dataset.materials),
                          reviewer=reviewer or next(
                              (q.reviewer for q in qrels if q.case_id == case.case_id), ""),
                          confirmed_at=date.today().isoformat())
               for case in sorted(dataset.cases, key=lambda item: item.case_id)]
    return qrels, reviews


def qrels_payload(qrels: list[Qrel], reviews: list[CaseReview]) -> dict[str, Any]:
    return {"qrels": [item.model_dump() for item in qrels],
            "reviews": [item.model_dump() for item in reviews]}

"""E9 盲标表：完整矩阵、CSV 往返与 qrel 回填校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xhs_agent.evaluation.labels import (
    build_qrels,
    export_rows,
    qrels_payload,
    read_csv,
    write_csv,
)
from xhs_agent.evaluation.models import Case, Dataset, DatasetManifest, MaterialAsset


def _dataset() -> Dataset:
    cases = [
        Case.model_validate({
            "case_id": f"case-{index}", "group_id": f"g{index}", "split": "dev",
            "input_type": "text", "natural_input": {"text": f"任务 {index}"},
            "expectation": {"answerable": True, "summary": "测试"},
            "oracle": {"elements": [{"type": "topic", "value": "早餐"}]},
            "scenario_tags": ["natural_naming"], "notes": "",
        })
        for index in (1, 2)
    ]
    materials = [
        MaterialAsset(material_key=f"mat-{index}", relative_path=f"assets/{index}.jpg",
                      sha256=str(index) * 64, media_type="image", source="s",
                      license="CC0", model_transfer_allowed=True)
        for index in (1, 2)
    ]
    return Dataset(
        manifest=DatasetManifest(version="v", protocol_version="1",
                                 dataset_kind="engineering_fake", status="draft"),
        materials=materials, cases=cases, qrels=[], reviews=[], e2e_tasks=[],
    )


def _rows() -> list[dict[str, str]]:
    rows = export_rows(_dataset())
    for row in rows:
        row.update({"grade": "0", "reason": "无关", "reviewer": "王老登",
                    "confirmed": "true"})
    return rows


def test_export_is_full_sorted_matrix() -> None:
    rows = export_rows(_dataset())
    assert [(row["case_id"], row["material_key"]) for row in rows] == [
        ("case-1", "mat-1"), ("case-1", "mat-2"),
        ("case-2", "mat-1"), ("case-2", "mat-2"),
    ]


def test_csv_roundtrip_keeps_unicode_and_header(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    rows = _rows()
    rows[0]["reason"] = "早餐直接可用"
    write_csv(path, rows)
    assert read_csv(path) == rows


def test_build_qrels_generates_full_reviews() -> None:
    qrels, reviews = build_qrels(_dataset(), _rows(), reviewer="王老登")
    assert len(qrels) == 4
    assert all(item.confirmed for item in qrels)
    assert len(reviews) == 2
    assert all(item.reviewed_materials == 2 for item in reviews)
    payload = qrels_payload(qrels, reviews)
    assert len(payload["qrels"]) == 4


def test_missing_cell_is_rejected() -> None:
    rows = _rows()[:-1]
    with pytest.raises(ValueError, match="缺 1 格"):
        build_qrels(_dataset(), rows, reviewer="王老登")


def test_invalid_grade_is_rejected() -> None:
    rows = _rows()
    rows[0]["grade"] = "3"
    with pytest.raises(ValueError, match="grade"):
        build_qrels(_dataset(), rows, reviewer="王老登")


def test_positive_grade_requires_reason() -> None:
    rows = _rows()
    rows[0].update({"grade": "2", "reason": ""})
    with pytest.raises(ValueError, match="必须写理由"):
        build_qrels(_dataset(), rows, reviewer="王老登")


def test_duplicate_and_unknown_rows_are_rejected() -> None:
    rows = _rows()
    rows[1] = dict(rows[0])
    with pytest.raises(ValueError, match="重复"):
        build_qrels(_dataset(), rows, reviewer="王老登")
    rows = _rows()
    rows[0]["case_id"] = "unknown"
    with pytest.raises(ValueError, match="未知"):
        build_qrels(_dataset(), rows, reviewer="王老登")


def test_missing_reviewer_is_rejected() -> None:
    rows = _rows()
    rows[0]["reviewer"] = ""
    with pytest.raises(ValueError, match="reviewer"):
        build_qrels(_dataset(), rows)

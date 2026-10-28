"""E9 数据包：hash、分区、qrel、全库审阅与冻结门。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from xhs_agent.evaluation.dataset import (
    CORE_FILES,
    load_and_validate,
    load_dataset,
    validate_dataset,
)
from xhs_agent.evaluation.models import DatasetManifest


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _base_payload() -> dict[str, object]:
    return {
        "materials": [
            {"material_key": "mat-a", "relative_path": "assets/a.jpg", "sha256": _sha(b"a"),
             "media_type": "image", "source": "synthetic", "license": "CC0",
             "model_transfer_allowed": True},
            {"material_key": "mat-b", "relative_path": "assets/b.jpg", "sha256": _sha(b"b"),
             "media_type": "image", "source": "synthetic", "license": "CC0",
             "model_transfer_allowed": True},
        ],
        "cases": [
            {"case_id": "case-answer", "group_id": "g1", "split": "dev",
             "input_type": "text", "natural_input": {"text": "周末想拍点治愈早餐"},
             "expectation": {"answerable": True, "summary": "找到早餐素材"},
             "oracle": {"elements": [{"type": "topic", "value": "早餐"}],
                        "match_keywords": ["早餐"]},
             "scenario_tags": ["natural_naming"], "notes": ""},
            {"case_id": "case-empty", "group_id": "g2", "split": "holdout",
             "input_type": "text", "natural_input": {"text": "想拍航天飞机发射"},
             "expectation": {"answerable": False, "summary": "明确缺口"},
             "oracle": {"elements": [{"type": "topic", "value": "航天飞机"}],
                        "match_keywords": ["航天飞机"]},
             "scenario_tags": ["vague"], "notes": ""},
        ],
        "qrels": {
            "qrels": [{"case_id": "case-answer", "material_key": "mat-a", "grade": 2,
                       "reason": "直接可用", "reviewer": "王老登", "confirmed": True}],
            "reviews": [
                {"case_id": "case-answer", "reviewed_materials": 2, "library_size": 2,
                 "reviewer": "王老登", "confirmed_at": "2026-10-28"},
                {"case_id": "case-empty", "reviewed_materials": 2, "library_size": 2,
                 "reviewer": "王老登", "confirmed_at": "2026-10-28"},
            ],
        },
        "e2e": {"tasks": []},
        "authorizations": {"material_keys": ["mat-a", "mat-b"]},
    }


def _write_pack(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "a.jpg").write_bytes(b"a")
    (root / "assets" / "b.jpg").write_bytes(b"b")
    payload = _base_payload()
    for name, key in (("materials.json", "materials"), ("cases.json", "cases")):
        _write_json(root / name, {key: payload[key]})
    _write_json(root / "qrels.json", payload["qrels"])
    _write_json(root / "e2e.json", payload["e2e"])
    _write_json(root / "authorizations.json", payload["authorizations"])
    files = {name: _sha((root / name).read_bytes()) for name in CORE_FILES}
    _write_json(root / "manifest.json", {
        "version": "e9-engineering",
        "protocol_version": "1",
        "dataset_kind": "engineering_fake",
        "status": "frozen",
        "frozen_at": "2026-10-28",
        "counts": {"cases": 2, "materials": 2},
        "files": files,
    })


def test_valid_engineering_pack_passes(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    dataset = load_dataset(tmp_path)
    report = validate_dataset(dataset, tmp_path, require_frozen=True)
    assert report.ok, report.errors
    assert report.counts["cases"] == 2


def test_tampered_case_file_is_rejected(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    path = tmp_path / "cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][0]["notes"] = "tampered"
    _write_json(path, payload)
    dataset, report = load_and_validate(tmp_path)
    assert dataset is not None
    assert any("hash" in item for item in report.errors)


def test_split_leak_is_rejected(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    path = tmp_path / "cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cases"][1]["group_id"] = "g1"
    _write_json(path, payload)
    dataset = load_dataset(tmp_path)
    report = validate_dataset(dataset, tmp_path)
    assert any("跨开发集" in item for item in report.errors)


def test_unanswerable_with_positive_qrel_is_rejected(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    path = tmp_path / "qrels.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["qrels"].append({"case_id": "case-empty", "material_key": "mat-b", "grade": 1,
                             "reason": "勉强", "reviewer": "王老登", "confirmed": True})
    _write_json(path, payload)
    dataset = load_dataset(tmp_path)
    report = validate_dataset(dataset, tmp_path)
    assert any("无答案" in item for item in report.errors)


def test_missing_review_is_rejected(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    path = tmp_path / "qrels.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["reviews"] = payload["reviews"][:1]
    _write_json(path, payload)
    dataset = load_dataset(tmp_path)
    report = validate_dataset(dataset, tmp_path)
    assert any("缺少全库审阅" in item for item in report.errors)


def test_absolute_material_path_is_rejected(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    path = tmp_path / "materials.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["materials"][0]["relative_path"] = "C:/private/a.jpg"
    _write_json(path, payload)
    dataset, report = load_and_validate(tmp_path)
    assert dataset is None
    assert any("数据包结构无效" in item for item in report.errors)


def test_draft_without_counts_only_warns(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "draft"
    _write_json(manifest_path, manifest)
    dataset = load_dataset(tmp_path)
    report = validate_dataset(dataset, tmp_path)
    assert report.ok
    assert any("draft" in item for item in report.warnings)


def test_manifest_model_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        DatasetManifest.model_validate({
            "version": "v", "protocol_version": "1", "dataset_kind": "e9",
            "status": "draft", "unknown": True,
        })

"""E9 CLI 离线命令：validate、estimate、freeze、report、check、score。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from xhs_agent.evaluation.cli import main


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _pack(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "a.jpg").write_bytes(b"a")
    materials = [{"material_key": "mat-a", "relative_path": "assets/a.jpg",
                  "sha256": _sha(b"a"), "media_type": "image", "source": "synthetic",
                  "license": "CC0", "model_transfer_allowed": True}]
    cases = [{"case_id": "case-answer", "group_id": "g1", "split": "dev",
              "input_type": "text", "natural_input": {"text": "测试"},
              "expectation": {"answerable": True, "summary": "测试"},
              "oracle": {"elements": [{"type": "topic", "value": "早餐"}],
                         "match_keywords": ["早餐"]},
              "scenario_tags": ["natural_naming"], "notes": ""}]
    _write_json(root / "materials.json", {"materials": materials})
    _write_json(root / "cases.json", {"cases": cases})
    _write_json(root / "qrels.json", {
        "qrels": [{"case_id": "case-answer", "material_key": "mat-a", "grade": 2,
                   "reason": "直接", "reviewer": "t", "confirmed": True}],
        "reviews": [{"case_id": "case-answer", "reviewed_materials": 1, "library_size": 1,
                     "reviewer": "t", "confirmed_at": "2026-10-28"}],
    })
    _write_json(root / "e2e.json", {"tasks": []})
    _write_json(root / "authorizations.json", {"material_keys": ["mat-a"]})
    _write_json(root / "prerequisites.json", {
        "s8_3_formal_cutover": {"complete": False},
        "s7_29_signed": {"complete": False},
    })
    core = ("materials.json", "cases.json", "qrels.json", "e2e.json", "authorizations.json")
    _write_json(root / "manifest.json", {
        "version": "e9-engineering", "protocol_version": "1",
        "dataset_kind": "engineering_fake", "status": "frozen",
        "frozen_at": "2026-10-28", "files": {name: _sha((root / name).read_bytes())
                                             for name in core},
    })


def test_validate_and_report(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["validate", "--experiment", str(tmp_path)]) == 0
    assert main(["report", "--experiment", str(tmp_path)]) == 0
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "not_run" in text


def test_estimate_is_offline_and_reports_budget(tmp_path: Path) -> None:
    _pack(tmp_path)
    code = main(["estimate", "--experiment", str(tmp_path)])
    assert code in (0, 3)


def test_freeze_candidate_writes_experiment(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["freeze-candidate", "--experiment", str(tmp_path)]) == 0
    data = json.loads((tmp_path / "experiment.json").read_text(encoding="utf-8"))
    assert data["data_version"] == "e9-engineering"
    assert data["candidate_digest"]


def test_evidence_check_returns_incomplete_without_attempts(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["check", "--experiment", str(tmp_path), "--gate", "evidence"]) == 4


def test_quality_check_returns_incomplete_without_attempts(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["check", "--experiment", str(tmp_path), "--gate", "quality"]) == 4


def test_score_import_writes_scores(tmp_path: Path) -> None:
    _pack(tmp_path)
    source = tmp_path / "scores.json"
    _write_json(source, {"scores": [{"attempt_id": "a1", "item": "semantic",
                                     "verdict": "pass", "reviewer": "t"}]})
    assert main(["score", "--experiment", str(tmp_path), "--import", str(source)]) == 0
    data = json.loads((tmp_path / "results" / "human_scores.json").read_text(encoding="utf-8"))
    assert data["scores"][0]["verdict"] == "pass"


def test_live_command_requires_live_flag(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["retrieval", "--experiment", str(tmp_path)]) == 3


def test_live_command_is_blocked_by_prerequisites(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["retrieval", "--experiment", str(tmp_path), "--live"]) == 4


def test_holdout_sweep_is_rejected(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["retrieval", "--experiment", str(tmp_path), "--live",
                 "--split", "holdout", "--sweep"]) == 3


def test_e2e_without_evidence_is_incomplete(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["e2e", "--experiment", str(tmp_path)]) == 4


def test_e2e_live_is_blocked_by_prerequisites(tmp_path: Path) -> None:
    _pack(tmp_path)
    assert main(["e2e", "--experiment", str(tmp_path), "--live"]) == 4

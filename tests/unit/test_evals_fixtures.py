"""评测集与示例素材包的冻结校验（S2.0）。

把"v1 冻结"变成可回归的约束：原地改用例或素材、引用不存在的素材、场景分布跑偏，
都会在这里变红。口径见 `docs/评测集.md`。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = PROJECT_ROOT / "evals"
DEMO_PACK = EVALS_DIR / "fixtures" / "demo_pack"

SCENE_DISTRIBUTION = {"明星运动": 5, "节目与影视梗": 4, "节日与节气": 3,
                      "生活方式": 5, "职场话题": 3}
ELEMENT_TYPES = {"ip", "topic", "scene", "visual", "emotion", "sound", "conflict",
                 "format", "audience"}
TOOL_EXPECTATIONS = {"no_vision_when_sidecar", "vision_when_no_hint", "recall_both_channels",
                     "repair_on_bad_json", "no_embedding_when_disabled", "no_candidate_padding"}
REQUIRED_CASE_FIELDS = {"id", "version", "scene", "hotspot", "clue_expectations",
                        "match_expectations", "notes"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((EVALS_DIR / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((EVALS_DIR / "cases").glob("case-*.json"))]


class TestManifest:
    def test_declares_frozen_version(self, manifest):
        assert manifest["version"] == "v1"
        assert manifest["frozen_at"]
        assert "升版" in manifest["usage"] or "v2" in manifest["usage"]

    def test_counts_match_reality(self, manifest, cases):
        counts = manifest["counts"]
        assets = [p for p in DEMO_PACK.rglob("*") if p.is_file() and p.suffix != ".txt"]
        assert counts["cases"] == len(cases) == 20
        assert counts["assets"] == len(assets) == 19
        assert counts["videos"] == sum(1 for p in assets if p.suffix == ".mp4") == 7
        assert counts["images"] == counts["assets"] - counts["videos"] == 12
        assert counts["files"] == len(manifest["files"])

    def test_every_registered_file_exists_and_hashes_match(self, manifest):
        """冻结的核心：登记在案的每个文件都必须字节一致。"""
        mismatched = []
        missing = []
        for rel, digest in manifest["files"].items():
            path = EVALS_DIR / rel
            if not path.is_file():
                missing.append(rel)
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != digest:
                mismatched.append(rel)
        assert missing == []
        assert mismatched == []

    def test_no_unregistered_files_slipped_in(self, manifest):
        registered = set(manifest["files"])
        actual = {path.relative_to(EVALS_DIR).as_posix()
                  for path in EVALS_DIR.rglob("*")
                  if path.is_file() and path.name not in {".gitkeep", "manifest.json"}
                  # evals/reports/ 是评测输出（不入 manifest、不属于冻结资产），见 docs/评测集.md §六
                  and not path.relative_to(EVALS_DIR).as_posix().startswith("reports/")}
        unexpected = sorted(actual - registered)
        assert not unexpected, f"这些文件没登记进 manifest：{unexpected}"


class TestCases:
    def test_scene_distribution_matches_product_doc(self, manifest, cases):
        scenes: dict[str, int] = {}
        for case in cases:
            scenes[case["scene"]] = scenes.get(case["scene"], 0) + 1
        assert scenes == SCENE_DISTRIBUTION
        assert manifest["scenes"] == SCENE_DISTRIBUTION

    def test_ids_are_unique_and_versioned(self, cases):
        ids = [case["id"] for case in cases]
        assert len(set(ids)) == len(ids) == 20
        assert {case["version"] for case in cases} == {"v1"}

    def test_required_fields_present(self, cases):
        for case in cases:
            missing = sorted(REQUIRED_CASE_FIELDS - set(case))
            assert not missing, f"{case['id']} 缺少字段：{missing}"
            assert case["hotspot"].strip(), case["id"]
            assert case["notes"].strip(), case["id"]

    def test_clue_expectations_use_contract_element_types(self, cases):
        for case in cases:
            clue = case["clue_expectations"]
            assert {"must", "allowed", "forbidden"} <= set(clue), case["id"]
            for bucket in ("must", "allowed", "forbidden"):
                for item in clue[bucket]:
                    assert item["type"] in ELEMENT_TYPES, (case["id"], item)
                    assert str(item["value"]).strip(), (case["id"], item)
            assert clue["must"], case["id"]

    def test_full_gap_cases_are_explicit(self, cases):
        full_gap = [case for case in cases if case.get("full_gap")]
        assert len(full_gap) == 2
        for case in full_gap:
            assert case["match_expectations"]["must_hit"] == [], case["id"]
            assert case["gap_expectations"], case["id"]
            assert "no_candidate_padding" in case.get("tool_expectations", []), case["id"]

    def test_normal_cases_annotate_at_least_one_must_hit(self, cases):
        for case in cases:
            if case.get("full_gap"):
                continue
            matches = case["match_expectations"]
            assert matches["must_hit"], case["id"]
            assert set(matches["must_hit"]) & set(matches["must_not"]) == set(), case["id"]

    def test_referenced_assets_all_exist_in_the_pack(self, cases):
        for case in cases:
            matches = case["match_expectations"]
            referenced = list(matches["must_hit"]) + list(matches["acceptable"]) \
                + list(matches["must_not"])
            for rel in referenced:
                assert (DEMO_PACK / rel).is_file(), (case["id"], rel)

    def test_tool_expectations_come_from_the_documented_enum(self, cases):
        for case in cases:
            for item in case.get("tool_expectations", []):
                assert item in TOOL_EXPECTATIONS, (case["id"], item)

    def test_budget_overrides_do_not_exceed_global_targets(self, cases):
        for case in cases:
            budget = case.get("budget")
            if not budget:
                continue
            assert budget["max_cost_cny"] <= 0.05, case["id"]
            assert budget["max_latency_s"] <= 60, case["id"]


class TestDemoPack:
    def test_every_asset_is_media_and_small(self):
        assets = [p for p in DEMO_PACK.rglob("*") if p.is_file() and p.suffix != ".txt"]
        assert {p.suffix for p in assets} <= {".mp4", ".jpg", ".png"}
        assert sum(p.stat().st_size for p in assets) < 3 * 1024 * 1024

    def test_sidecars_are_paired_with_media(self):
        sidecars = list(DEMO_PACK.rglob("*.txt"))
        assert len(sidecars) == 16
        for sidecar in sidecars:
            media = Path(str(sidecar)[:-4])
            assert media.is_file(), sidecar.name
            assert "标题:" in sidecar.read_text(encoding="utf-8")

    def test_messy_files_are_intentionally_undocumented(self):
        messy = sorted(path.name for path in (DEMO_PACK / "未整理").iterdir())
        assert messy == ["IMG_20260901_143012.jpg", "临时素材.mp4", "视频1.mp4"]

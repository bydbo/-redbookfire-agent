"""评测脚本的纯函数单测（S2.8）：按路径加载脚本模块，不连库、不联网、不需要密钥。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from xhs_agent.schemas import Element, HotspotClue, Material

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eval_retrieval.py"


@pytest.fixture(scope="module")
def ev():
    """按路径加载评测脚本（`if __name__ == "__main__"` 守护，导入无副作用）。"""
    spec = importlib.util.spec_from_file_location("eval_retrieval_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_material(path: str, **overrides) -> Material:
    base = {"id": path, "path": path}
    base.update(overrides)
    return Material(**base)


def make_clue(*, keywords=("羽毛球", "球场"), elements=(("topic", "羽毛球"), ("scene", "球场"))):
    return HotspotClue(hotspot_raw="某明星打羽毛球",
                       elements=[Element(type=etype, value=value) for etype, value in elements],
                       match_keywords=list(keywords))


def make_case(case_id: str = "case-01", *, must=("运动/球场-挥拍.mp4",), acceptable=(),
              must_not=(), scene: str = "明星运动") -> dict:
    return {
        "id": case_id,
        "version": "v1",
        "scene": scene,
        "hotspot": "某明星打羽毛球被拍",
        "clue_expectations": {
            "must": [{"type": "topic", "value": "羽毛球"}],
            "allowed": [{"type": "scene", "value": "球场"}],
            "forbidden": [],
        },
        "match_expectations": {
            "must_hit": list(must),
            "acceptable": list(acceptable),
            "must_not": list(must_not),
        },
        "notes": "用例单测",
    }


class TestBaseline:
    def test_patterns_come_from_keywords_and_element_values(self, ev):
        clue = make_clue()
        assert ev.baseline_patterns(clue) == ["羽毛球", "球场"]

    def test_ranks_by_matched_pattern_count_then_path(self, ev):
        clue = make_clue()
        both = make_material("D:/pack/运动/球场-挥拍.mp4", title="球场挥拍",
                             description="羽毛球对打")
        one = make_material("D:/pack/运动/球拍特写.jpg", title="羽毛球拍特写")
        none = make_material("D:/pack/美食/咖啡.jpg", title="手冲咖啡")
        ranked = ev.keyword_baseline_rank(clue, [none, one, both], 5)
        assert [material.path for material in ranked] == [both.path, one.path]

    def test_filename_alone_is_enough(self, ev):
        clue = make_clue()
        material = make_material("D:/pack/运动/球场-挥拍.mp4")
        ranked = ev.keyword_baseline_rank(clue, [material], 5)
        assert [item.path for item in ranked] == [material.path]

    def test_ties_break_by_path(self, ev):
        clue = make_clue()
        materials = [make_material("D:/pack/b.mp4", title="羽毛球"),
                     make_material("D:/pack/a.mp4", title="羽毛球")]
        ranked = ev.keyword_baseline_rank(clue, materials, 5)
        assert [item.path for item in ranked] == ["D:/pack/a.mp4", "D:/pack/b.mp4"]

    def test_respects_topk(self, ev):
        clue = make_clue()
        materials = [make_material(f"D:/pack/{index}.mp4", title="羽毛球")
                     for index in range(4)]
        assert len(ev.keyword_baseline_rank(clue, materials, 2)) == 2

    def test_without_patterns_returns_empty(self, ev):
        clue = make_clue(keywords=(), elements=())
        assert ev.keyword_baseline_rank(clue, [make_material("D:/pack/a.mp4")], 5) == []


class TestOracleClue:
    def test_uses_must_and_allowed_with_schema_defaults(self, ev):
        clue = ev.build_oracle_clue(make_case())
        assert [element.value for element in clue.elements] == ["羽毛球", "球场"]
        assert clue.elements[0].weight == pytest.approx(0.6)
        assert clue.elements[0].confidence == pytest.approx(0.7)
        assert clue.match_keywords == ["羽毛球", "球场"]
        assert clue.hotspot_raw == "某明星打羽毛球被拍"

    def test_correct_paths_include_acceptable_and_exclude_must_not(self, ev):
        case = make_case(acceptable=("运动/球拍特写.jpg",), must_not=("美食/家常菜.jpg",))
        assert ev.correct_paths(case) == {"运动/球场-挥拍.mp4", "运动/球拍特写.jpg"}

    def test_full_gap_detection(self, ev):
        assert ev.is_full_gap(make_case(must=())) is True
        assert ev.is_full_gap(make_case()) is False


class TestScoreCase:
    def test_marks_hits_and_must_not_violations(self, ev):
        case = make_case(acceptable=("运动/球拍特写.jpg",), must_not=("美食/家常菜.jpg",))
        item = ev.score_case(case, ["美食/家常菜.jpg", "运动/球拍特写.jpg"],
                             coverage_ratio=0.5, latency_ms=12, cost_cny=0.000012, topk=5)
        assert item["top5_hit"] is True        # acceptable 计入命中
        assert item["first_hit"] is False      # 首选是 must_not 素材
        assert item["must_not_hits"] == ["美食/家常菜.jpg"]
        assert item["cost_cny"] == pytest.approx(0.000012)
        assert item["coverage_ratio"] == pytest.approx(0.5)

    def test_topk_truncation_decides_top5_hit(self, ev):
        case = make_case(must=("运动/球场-挥拍.mp4",))
        ranked = ["a.mp4", "b.mp4", "c.mp4", "d.mp4", "运动/球场-挥拍.mp4"]
        assert ev.score_case(case, ranked, coverage_ratio=None, latency_ms=1,
                             cost_cny=0.0, topk=3)["top5_hit"] is False
        assert ev.score_case(case, ranked, coverage_ratio=None, latency_ms=1,
                             cost_cny=0.0, topk=5)["top5_hit"] is True

    def test_full_gap_case_has_no_hit_flags(self, ev):
        item = ev.score_case(make_case(must=()), [], coverage_ratio=0.0, latency_ms=3,
                             cost_cny=0.0, topk=5)
        assert item["full_gap"] is True
        assert item["top5_hit"] is None
        assert item["first_hit"] is None
        assert item["coverage_ratio"] == 0.0


class TestAggregateMetrics:
    def test_excludes_skipped_cases_from_hit_rates_but_counts_coverage(self, ev):
        results = [
            {"id": "a", "top5_hit": True, "first_hit": True, "coverage_ratio": 1.0,
             "latency_ms": 10, "cost_cny": 0.0, "must_not_hits": []},
            {"id": "b", "top5_hit": False, "first_hit": False, "coverage_ratio": 0.0,
             "latency_ms": 20, "cost_cny": 0.000002, "must_not_hits": []},
            {"id": "c", "top5_hit": None, "first_hit": None, "coverage_ratio": 0.5,
             "latency_ms": 30, "cost_cny": 0.0, "must_not_hits": ["x.mp4"]},
        ]
        metrics = ev.aggregate_metrics(results)
        assert (metrics["cases"], metrics["scored_cases"]) == (3, 2)
        assert metrics["top5_hit_rate"] == 0.5
        assert metrics["first_hit_rate"] == 0.5
        assert metrics["mean_coverage"] == pytest.approx(0.5)   # 含被跳过的用例
        assert metrics["mean_latency_ms"] == 20.0
        assert metrics["max_latency_ms"] == 30
        assert metrics["mean_cost_cny"] == pytest.approx(0.00000067)
        assert metrics["must_not_violations"] == 1

    def test_empty_results_do_not_raise(self, ev):
        metrics = ev.aggregate_metrics([])
        assert metrics["top5_hit_rate"] is None
        assert metrics["mean_coverage"] is None
        assert metrics["mean_latency_ms"] is None
        assert metrics["must_not_violations"] == 0

    def test_rates_are_rounded_to_four_decimals(self, ev):
        results = [{"id": str(index), "top5_hit": index == 0, "first_hit": index == 0,
                    "coverage_ratio": None, "latency_ms": 1, "cost_cny": 0.0,
                    "must_not_hits": []} for index in range(3)]
        assert ev.aggregate_metrics(results)["top5_hit_rate"] == 0.3333


class TestHelpersAndFrozenData:
    def test_relative_pack_path_normalizes_separators(self, ev):
        pack = Path("D:/project/evals/fixtures/demo_pack")
        path = str(pack / "运动" / "球场-挥拍.mp4")
        assert ev.relative_pack_path(path, pack) == "运动/球场-挥拍.mp4"

    def test_loads_the_frozen_v1_cases(self, ev):
        cases = ev.load_cases()
        assert len(cases) == 20
        assert {case["version"] for case in cases} == {"v1"}

    def test_full_gap_cases_in_v1_are_the_expected_two(self, ev):
        assert {case["id"] for case in ev.load_cases() if ev.is_full_gap(case)} == \
            {"case-04", "case-11"}

"""检索工具单测：查询串口径、RRF 融合、终分公式与排序截断（全离线）。"""

from __future__ import annotations

import pytest

from xhs_agent.schemas import Element, HotspotClue
from xhs_agent.tools import retrieval

NOW = 1_700_000_000.0
DAY = 86400.0


def bare_clue(elements, *, raw: str = "某明星打羽毛球", **overrides) -> HotspotClue:
    """显式清空 `match_keywords` 的线索。

    `matching` 有一条已知行为：线索级 `match_keywords` 命中会给**每个**要素加 0.9 相似度、
    不区分要素类型（见 `tests/unit/test_matching.py::test_keyword_hit_is_not_type_aware`）。
    要单独验证「要素类型对齐」这条路径时，必须先把关键词清空。
    """
    return HotspotClue(hotspot_raw=raw,
                       elements=[Element(type=etype, value=value) for etype, value in elements],
                       match_keywords=[], **overrides)


def params(**overrides) -> retrieval.RetrievalParams:
    base = {
        "similarity_threshold": 0.2,
        "recall_limit": 50,
        "max_cosine_distance": 0.35,
        "rrf_k": 60,
        "w_element": 0.7,
        "w_rrf": 0.3,
        "hit_threshold": 0.5,
        "topk": 5,
        "min_score": 0.15,
        "element_type_weights": {},
    }
    base.update(overrides)
    return retrieval.RetrievalParams(**base)


class TestQueryText:
    def test_literal_query_is_keywords_then_element_values(self, make_clue):
        clue = make_clue(elements=[("topic", "羽毛球"), ("scene", "球场")],
                         match_keywords=["羽毛球", "挥拍"])
        assert retrieval.literal_query_text(clue) == "羽毛球 挥拍 球场"

    def test_literal_query_dedupes_and_drops_blanks(self, make_clue):
        clue = make_clue(elements=[("topic", "羽毛球")],
                         match_keywords=["羽毛球", "  ", "羽毛球"])
        assert retrieval.literal_query_text(clue) == "羽毛球"

    def test_vector_query_is_raw_why_then_mechanisms(self, make_clue):
        clue = make_clue(raw="明星打羽毛球", why_it_works=["反差感"],
                         mechanisms=[{"name": "反差", "explain": "身份反差"}])
        assert retrieval.vector_query_text(clue) == "明星打羽毛球 反差感 反差 身份反差"

    def test_vector_query_without_extras_is_just_raw(self, make_clue):
        clue = make_clue(raw="节后减脂", why_it_works=[], mechanisms=[])
        assert retrieval.vector_query_text(clue) == "节后减脂"


class TestRrf:
    def test_accumulates_across_channels(self):
        scores = retrieval.rrf_scores(
            {"literal": {"a": 1, "b": 2}, "vector": {"b": 1}}, k=60)
        assert scores["a"] == pytest.approx(1 / 61)
        assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)

    def test_normalize_puts_max_at_one(self):
        normalized = retrieval.normalize_rrf({"a": 2.0, "b": 1.0})
        assert normalized == {"a": 1.0, "b": 0.5}

    def test_normalize_empty_and_zero(self):
        assert retrieval.normalize_rrf({}) == {}
        assert retrieval.normalize_rrf({"a": 0.0}) == {"a": 0.0}


class TestFinalScore:
    def test_weighted_sum_times_quality(self):
        score = retrieval.final_score(1.0, 1.0, quality=1.0, recency=0.0,
                                      params=params(w_element=0.7, w_rrf=0.3))
        assert score == pytest.approx(1.0)
        low_quality = retrieval.final_score(1.0, 1.0, quality=0.4, recency=0.0,
                                           params=params(w_element=0.7, w_rrf=0.3))
        assert low_quality == pytest.approx(0.94)

    def test_weights_shift_share_between_element_and_rrf(self):
        element_only = retrieval.final_score(1.0, 0.0, quality=1.0, recency=0.0,
                                            params=params(w_element=1.0, w_rrf=0.0))
        rrf_only = retrieval.final_score(0.0, 0.0, quality=1.0, recency=0.0,
                                        params=params(w_element=1.0, w_rrf=0.0))
        assert element_only > rrf_only

    def test_recency_is_added_then_clamped(self):
        assert retrieval.final_score(0.0, 0.0, quality=1.0, recency=0.2,
                                     params=params()) == 0.2
        assert retrieval.final_score(1.0, 1.0, quality=1.0, recency=0.2,
                                     params=params()) == 1.0


class TestRankCandidates:
    def test_sorts_by_score_desc_and_rounds(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        hit = make_material("hit", tags=["羽毛球"])
        miss = make_material("miss", tags=["咖啡"])
        candidates, _coverage = retrieval.rank_candidates(
            clue, [miss, hit], {"literal": {"hit": 1, "miss": 2}}, params(), now=NOW)
        assert [item.material_id for item in candidates] == ["hit", "miss"]
        assert [item.rank for item in candidates] == [1, 2]
        assert all(item.score == round(item.score, 4) for item in candidates)
        assert candidates[0].score > candidates[1].score

    def test_same_score_breaks_tie_by_material_id(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        # 两条素材字段完全相同、各占一个通道的第 1 名 → RRF 相同 → 同分
        first = make_material("b", tags=["羽毛球"])
        second = make_material("a", tags=["羽毛球"])
        candidates, _coverage = retrieval.rank_candidates(
            clue, [first, second], {"literal": {"b": 1}, "vector": {"a": 1}},
            params(), now=NOW)
        assert [item.material_id for item in candidates] == ["a", "b"]
        assert candidates[0].score == candidates[1].score

    def test_skips_material_without_any_channel(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        recalled = make_material("kept", tags=["羽毛球"])
        orphan = make_material("orphan", tags=["羽毛球"])
        candidates, _coverage = retrieval.rank_candidates(
            clue, [recalled, orphan], {"literal": {"kept": 1}}, params(), now=NOW)
        assert [item.material_id for item in candidates] == ["kept"]

    def test_recall_sources_are_canonical_order(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        both = make_material("both", tags=["羽毛球"])
        only_vector = make_material("vec", tags=["羽毛球"])
        candidates, _coverage = retrieval.rank_candidates(
            clue, [both, only_vector],
            {"vector": {"both": 1, "vec": 2}, "literal": {"both": 1}}, params(), now=NOW)
        by_id = {item.material_id: item.recall_sources for item in candidates}
        assert by_id["both"] == ["literal", "vector"]
        assert by_id["vec"] == ["vector"]

    def test_truncates_to_topk(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        materials = [make_material(f"m{i}", tags=["羽毛球"]) for i in range(3)]
        ranks = {"literal": {f"m{i}": i + 1 for i in range(3)}}
        candidates, _coverage = retrieval.rank_candidates(
            clue, materials, ranks, params(topk=2), now=NOW)
        assert len(candidates) == 2
        assert [item.rank for item in candidates] == [1, 2]

    def test_min_score_cuts_output_but_coverage_binds_to_recall_set(self, make_material):
        """`min_score` 只裁 Top-K 输出；覆盖度绑定的是**召回集**（《检索契约》§七）。"""
        clue = bare_clue([("topic", "羽毛球")])
        material = make_material("m", tags=["羽毛球"])
        candidates, coverage = retrieval.rank_candidates(
            clue, [material], {"literal": {"m": 1}}, params(min_score=1.0), now=NOW)
        assert candidates == []
        assert coverage.ratio == 1.0  # 库里找得到，只是没达到返回阈值

    def test_min_score_cuts_the_tail(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        hit = make_material("hit", tags=["羽毛球"])
        weak = make_material("weak", tags=["咖啡"])
        candidates, _coverage = retrieval.rank_candidates(
            clue, [hit, weak], {"literal": {"hit": 1, "weak": 2}},
            params(min_score=0.5), now=NOW)
        assert [item.material_id for item in candidates] == ["hit"]

    def test_reasons_are_filled_and_candidate_is_final(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        material = make_material("m", tags=["羽毛球"], title="球场热身")
        candidates, _coverage = retrieval.rank_candidates(
            clue, [material], {"literal": {"m": 1}}, params(), now=NOW)
        assert candidates[0].reasons
        assert candidates[0].usage
        candidates[0].finalize()  # 终态：再校验一次不抛错

    def test_coverage_counts_hit_elements(self, make_material):
        clue = bare_clue([("topic", "羽毛球"), ("scene", "月球")])
        material = make_material("m", tags=["羽毛球"])
        _candidates, coverage = retrieval.rank_candidates(
            clue, [material], {"literal": {"m": 1}}, params(), now=NOW)
        assert [element.value for element in coverage.covered] == ["羽毛球"]
        assert [element.value for element in coverage.gaps] == ["月球"]
        assert 0.0 < coverage.ratio < 1.0

    def test_empty_recall_set_returns_empty_with_gaps(self, make_clue):
        clue = make_clue(elements=[("topic", "羽毛球")])
        candidates, coverage = retrieval.rank_candidates(clue, [], {}, params(), now=NOW)
        assert candidates == []
        assert coverage.ratio == 0.0
        assert [element.value for element in coverage.gaps] == ["羽毛球"]

    def test_recency_bonus_is_injectable(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        fresh = make_material("m", tags=["羽毛球"], mtime=NOW - 10 * DAY)
        stale = make_material("m", tags=["羽毛球"], mtime=NOW - 400 * DAY)
        ranks = {"literal": {"m": 1}}
        fresh_score = retrieval.rank_candidates(clue, [fresh], ranks, params(), now=NOW)[0][0].score
        stale_score = retrieval.rank_candidates(clue, [stale], ranks, params(), now=NOW)[0][0].score
        assert fresh_score - stale_score == pytest.approx(0.05)

    def test_is_stable_across_calls(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        materials = [make_material(f"m{i}", tags=["羽毛球"]) for i in range(3)]
        ranks = {"literal": {"m0": 1, "m1": 2, "m2": 3}}
        first, first_coverage = retrieval.rank_candidates(clue, materials, ranks, params(), now=NOW)
        second, second_coverage = retrieval.rank_candidates(clue, materials, ranks, params(), now=NOW)
        assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
        assert first_coverage.to_dict() == second_coverage.to_dict()

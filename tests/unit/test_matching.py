"""匹配打分单测：分数构成、排序截断、覆盖度、解释补齐与输出稳定性。"""

from __future__ import annotations

import pytest

from xhs_agent.schemas import Element, HotspotClue
from xhs_agent.tools import matching

NOW = 1_700_000_000.0
DAY = 86400.0


class TestScoreMaterial:
    def test_exact_match_produces_hit(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        score, hits, missing = matching.score_material(
            clue, make_material(tags=["羽毛球"], title="球场热身"), now=NOW)
        assert [hit.element_type for hit in hits] == ["topic"]
        assert missing == []
        # 关键词命中给到 0.9 的相似度上限，再乘画质系数（无宽高 = 0.4）
        assert score > 0.8

    def test_unrelated_material_has_no_hit(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        score, hits, missing = matching.score_material(
            clue, make_material(tags=["咖啡"], title="手冲咖啡"), now=NOW)
        assert hits == []
        assert len(missing) == 1
        assert score < 0.5

    def test_material_text_joins_tags_title_description(self, make_material):
        material = make_material(tags=["a", "b"], title="标题", description="描述")
        assert matching.material_text(material) == "a b 标题 描述"

    def test_quality_score_raises_total(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        plain = matching.score_material(clue, make_material(tags=["羽毛球"]), now=NOW)[0]
        hd = matching.score_material(clue, make_material(
            tags=["羽毛球"], width=1080, height=1920, duration_s=15), now=NOW)[0]
        assert hd > plain

    def test_type_weights_shift_share(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球"), ("ip", "顶流明星")])
        material = make_material(tags=["羽毛球"])
        baseline = matching.score_material(clue, material, now=NOW)[0]
        # 把没命中的 ip 要素权重抬高，命中要素的占比下降 → 总分下降
        penalized = matching.score_material(
            clue, material, type_weights={"ip": 9.0}, now=NOW)[0]
        assert penalized < baseline

    def test_no_mtime_means_no_recency_adjustment(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        base = matching.score_material(clue, make_material(tags=["羽毛球"]), now=NOW)[0]
        fresh = matching.score_material(
            clue, make_material(tags=["羽毛球"], mtime=NOW - 10 * DAY), now=NOW)[0]
        stale = matching.score_material(
            clue, make_material(tags=["羽毛球"], mtime=NOW - 400 * DAY), now=NOW)[0]
        assert fresh - base == pytest.approx(0.02)
        assert base - stale == pytest.approx(0.03)

    def test_now_is_the_recency_reference(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        material = make_material(tags=["羽毛球"], mtime=NOW)
        fresh = matching.score_material(clue, material, now=NOW)[0]
        stale = matching.score_material(clue, material, now=NOW + 400 * DAY)[0]
        assert fresh > stale


class TestRankMaterials:
    def test_sorted_by_score_desc(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球"), ("scene", "球场")])
        materials = [
            make_material(material_id="miss", path="a.mp4", tags=["咖啡"]),
            make_material(material_id="hit", path="z.mp4", tags=["羽毛球", "球场"]),
        ]
        candidates, coverage = matching.rank_materials(clue, materials, now=NOW)
        assert [c.material_id for c in candidates] == ["hit", "miss"]
        assert coverage.ratio > 0

    def test_topk_truncates_and_ranks_sequentially(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        materials = [make_material(material_id=f"m{i}", path=f"{i}.mp4", tags=["羽毛球"])
                     for i in range(5)]
        candidates, _coverage = matching.rank_materials(clue, materials, topk=2, now=NOW)
        assert [c.rank for c in candidates] == [1, 2]

    def test_min_score_cuts_the_tail(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        materials = [
            make_material(material_id="hit", path="hit.mp4", tags=["羽毛球"]),
            make_material(material_id="miss", path="miss.mp4", tags=["咖啡"]),
        ]
        candidates, _coverage = matching.rank_materials(clue, materials, min_score=0.5, now=NOW)
        assert [c.material_id for c in candidates] == ["hit"]

    def test_ties_break_by_path(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        materials = [
            make_material(material_id="z", path="z.mp4", tags=["羽毛球"]),
            make_material(material_id="a", path="a.mp4", tags=["羽毛球"]),
        ]
        candidates, _coverage = matching.rank_materials(clue, materials, now=NOW)
        assert [c.material.path for c in candidates] == ["a.mp4", "z.mp4"]

    def test_coverage_splits_covered_and_gaps(self, make_clue, make_material):
        # 关键词命中是「整条素材」的证据、不区分要素类型，这里显式清空关键词，
        # 单独验证类型对齐的匹配路径。
        clue = HotspotClue(
            hotspot_raw="某顶流明星打羽毛球被拍",
            elements=[Element(type="topic", value="羽毛球"),
                      Element(type="ip", value="顶流明星")],
            match_keywords=[],
        )
        _candidates, coverage = matching.rank_materials(
            clue, [make_material(tags=["羽毛球"])], now=NOW)
        assert [e.value for e in coverage.covered] == ["羽毛球"]
        assert [e.value for e in coverage.gaps] == ["顶流明星"]
        assert 0 < coverage.ratio < 1

    def test_keyword_hit_is_not_type_aware(self, make_clue, make_material):
        """已知行为（不是期望行为）：命中关键词会给**每个**线索要素加 0.9 相似度。

        于是「素材只提到羽毛球」也会让 IP 要素「顶流明星」算作已覆盖。
        是否收紧属于排序口径变更，需要单独评估（会改动检索契约里的打分结果）。
        """
        clue = make_clue(elements=[("topic", "羽毛球"), ("ip", "顶流明星")])
        assert clue.match_keywords == ["羽毛球", "顶流明星"]
        _candidates, coverage = matching.rank_materials(
            clue, [make_material(tags=["羽毛球"])], now=NOW)
        assert [e.value for e in coverage.covered] == ["羽毛球", "顶流明星"]

    def test_candidates_are_drafts_until_explained(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        candidates, _coverage = matching.rank_materials(
            clue, [make_material(tags=["羽毛球"])], now=NOW)
        assert candidates[0].reasons == []


class TestExplainAndDedupe:
    def test_explain_fills_reasons_usage_and_finalizes(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        candidates, _coverage = matching.rank_materials(
            clue, [make_material(tags=["羽毛球"])], now=NOW)
        matching.explain_candidates(clue, candidates)
        assert candidates[0].reasons
        assert candidates[0].usage
        assert candidates[0].finalize() is candidates[0]

    def test_dedupe_keeps_first_and_ignores_case(self, make_material):
        kept = matching.dedupe_by_path([
            make_material(material_id="a", path="D:/M/clip.mp4"),
            make_material(material_id="b", path="d:/m/CLIP.mp4"),
            make_material(material_id="c", path="D:/M/other.mp4"),
        ])
        assert [m.id for m in kept] == ["a", "c"]


class TestDeterminism:
    """工具标准第 ③ 条：同一输入必须得到同一输出（时间基准显式注入）。"""

    def test_same_input_same_result(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球"), ("scene", "球场")])
        materials = [make_material(tags=["羽毛球", "球场"], mtime=NOW - 5 * DAY),
                     make_material(material_id="m_2", path="b.mp4", tags=["咖啡"])]
        first, coverage_a = matching.rank_materials(clue, materials, now=NOW)
        second, coverage_b = matching.rank_materials(clue, materials, now=NOW)
        assert [c.to_dict() for c in first] == [c.to_dict() for c in second]
        assert coverage_a.to_dict() == coverage_b.to_dict()

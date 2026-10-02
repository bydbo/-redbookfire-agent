"""要素级匹配与规则解释单测：相似度、时效、解释补齐、去重与输出稳定性。

排序、平局、截断、覆盖度与终分属于检索层，见 `tests/unit/test_retrieval_tools.py`。
"""

from __future__ import annotations

import pytest

from xhs_agent.schemas import Element, HotspotClue, MatchCandidate
from xhs_agent.tools import matching

NOW = 1_700_000_000.0
DAY = 86400.0


class TestMaterialText:
    def test_joins_tags_title_description(self, make_material):
        material = make_material(tags=["a", "b"], title="标题", description="描述")
        assert matching.material_text(material) == "a b 标题 描述"


class TestElementRelevance:
    def test_exact_match_produces_hit(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        relevance, hits, missing = matching.element_relevance(
            clue, make_material(tags=["羽毛球"], title="球场热身"))
        assert [hit.element_type for hit in hits] == ["topic"]
        assert missing == []
        assert relevance > 0.8

    def test_unrelated_material_has_no_hit(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        relevance, hits, missing = matching.element_relevance(
            clue, make_material(tags=["咖啡"], title="手冲咖啡"))
        assert hits == []
        assert len(missing) == 1
        assert relevance < 0.5

    def test_type_weights_shift_share(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球"), ("ip", "顶流明星")])
        material = make_material(tags=["羽毛球"])
        baseline = matching.element_relevance(clue, material)[0]
        # 把没命中的 ip 要素权重抬高，命中要素的占比下降 → 相关度下降
        penalized = matching.element_relevance(clue, material, type_weights={"ip": 9.0})[0]
        assert penalized < baseline

    def test_match_elements_does_not_apply_hit_threshold(self, make_clue, make_material):
        """`match_elements` 只算相似度、不按阈值裁剪——命中/缺口由 `split_hits` 切。"""
        clue = make_clue(elements=[("topic", "羽毛球运动")])
        matches = matching.match_elements(clue, make_material(tags=["羽毛球场"]))
        assert 0.0 < matches[0].similarity < matching.HIT_THRESHOLD
        hits, missing = matching.split_hits(matches)
        assert hits == []
        assert [element.value for element in missing] == ["羽毛球运动"]

    def test_element_evidence_is_used_as_surface(self, make_material):
        clue = HotspotClue(hotspot_raw="某明星挥拍", match_keywords=[],
                           elements=[Element(type="topic", value="羽毛球", evidence="挥拍")])
        _relevance, hits, _missing = matching.element_relevance(
            clue, make_material(title="球场挥拍集锦"))
        assert [hit.hit_value for hit in hits] == ["关键词「挥拍」命中"]

    def test_keyword_hit_is_not_type_aware(self, make_clue, make_material):
        """已知行为（不是期望行为）：命中关键词会给**每个**线索要素加 0.9 相似度。

        于是「素材只提到羽毛球」也会让 IP 要素「顶流明星」算作已命中，覆盖度随之偏高。
        是否收紧属于排序口径变更，需要单独评估（会改动检索契约里的打分结果）。
        """
        clue = make_clue(elements=[("topic", "羽毛球"), ("ip", "顶流明星")])
        assert clue.match_keywords == ["羽毛球", "顶流明星"]
        _relevance, hits, missing = matching.element_relevance(
            clue, make_material(tags=["羽毛球"]))
        assert [hit.clue_value for hit in hits] == ["羽毛球", "顶流明星"]
        assert missing == []


class TestRecencyBonus:
    def test_no_mtime_means_no_adjustment(self, make_material):
        assert matching.recency_bonus(make_material(), now=NOW) == 0.0

    def test_fresh_material_gets_bonus(self, make_material):
        material = make_material(mtime=NOW - 10 * DAY)
        assert matching.recency_bonus(material, now=NOW) == pytest.approx(0.02)

    def test_stale_material_is_penalized(self, make_material):
        material = make_material(mtime=NOW - 400 * DAY)
        assert matching.recency_bonus(material, now=NOW) == pytest.approx(-0.03)

    def test_middle_age_is_neutral(self, make_material):
        material = make_material(mtime=NOW - 100 * DAY)
        assert matching.recency_bonus(material, now=NOW) == 0.0


class TestExplainAndDedupe:
    def test_explain_fills_reasons_and_usage_from_hits(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        material = make_material(tags=["羽毛球"], title="球场热身", duration_s=15)
        matches = matching.match_elements(clue, material)
        hits, missing = matching.split_hits(matches)
        candidate = MatchCandidate.draft(material_id=material.id, material=material,
                                         hits=hits, missing=missing, rank=1)
        assert candidate.reasons == []  # 中间态：解释前允许为空

        matching.explain_candidates(clue, [candidate])
        assert candidate.reasons[0].startswith("命中主题：羽毛球")
        assert "关键词「羽毛球」命中" in candidate.reasons[0]
        assert candidate.usage == ("可用作羽毛球的实拍素材，建议放在开头 3 秒或作为过程画面"
                                   "（时长 15.0 秒）")
        assert candidate.finalize() is candidate

    def test_explain_falls_back_when_nothing_hit(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球")])
        candidate = MatchCandidate.draft(material_id="m_1", material=make_material())
        matching.explain_candidates(clue, [candidate])
        assert candidate.reasons == ["没有明显命中，只是兜底候选"]
        assert candidate.usage.startswith("可用作羽毛球的实拍素材")

    def test_explain_keeps_reasons_from_other_sources(self, make_clue, make_material):
        """已有 reasons / usage（例如模型给的）不被规则解释覆盖，只做终态校验。"""
        clue = make_clue(elements=[("topic", "羽毛球")])
        candidate = MatchCandidate(material_id="m_1", material=make_material(),
                                   reasons=["模型给的理由"], usage="放开头 3 秒")
        matching.explain_candidates(clue, [candidate])
        assert candidate.reasons == ["模型给的理由"]
        assert candidate.usage == "放开头 3 秒"

    def test_dedupe_keeps_first_and_ignores_case(self, make_material):
        kept = matching.dedupe_by_path([
            make_material(material_id="a", path="D:/M/clip.mp4"),
            make_material(material_id="b", path="d:/m/CLIP.mp4"),
            make_material(material_id="c", path="D:/M/other.mp4"),
        ])
        assert [material.id for material in kept] == ["a", "c"]


class TestDeterminism:
    """工具标准第 ③ 条：同一输入必须得到同一输出（时间基准显式注入）。"""

    def test_same_input_same_result(self, make_clue, make_material):
        clue = make_clue(elements=[("topic", "羽毛球"), ("scene", "球场")])
        materials = [make_material(tags=["羽毛球", "球场"], mtime=NOW - 5 * DAY),
                     make_material(material_id="m_2", path="b.mp4", tags=["咖啡"])]
        first = [(matching.element_relevance(clue, material)[0],
                  matching.recency_bonus(material, now=NOW)) for material in materials]
        second = [(matching.element_relevance(clue, material)[0],
                   matching.recency_bonus(material, now=NOW)) for material in materials]
        assert first == second

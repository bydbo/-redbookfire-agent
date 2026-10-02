"""缺口建议单测：建议表覆盖全部要素类型、顺序、空缺口（S3.1）。"""

from __future__ import annotations

import pytest

from xhs_agent.schemas import ELEMENT_TYPES, TYPE_LABELS, Coverage, Element
from xhs_agent.tools import gaps


class TestGapAdvice:
    def test_advice_covers_every_element_type(self):
        """建议表必须覆盖全部要素类型——否则报告会退到通用文案（兜底只在枚举扩张时用）。"""
        assert set(gaps.ADVICE_BY_TYPE) == set(ELEMENT_TYPES) == set(TYPE_LABELS)
        assert gaps.DEFAULT_ADVICE not in gaps.ADVICE_BY_TYPE.values()

    def test_returns_one_entry_per_gap_with_label_and_advice(self):
        coverage = Coverage(ratio=0.0, covered=[], gaps=[
            Element(type="ip", value="明星艺人"),
            Element(type="sound", value="现场欢呼"),
        ])
        advice = gaps.gap_advice(coverage)
        assert [item["type"] for item in advice] == ["ip", "sound"]
        assert [item["value"] for item in advice] == ["明星艺人", "现场欢呼"]
        assert advice[0]["label"] == TYPE_LABELS["ip"]
        assert advice[0]["advice"] == gaps.ADVICE_BY_TYPE["ip"]
        assert "肖像" in advice[0]["advice"]
        assert "授权" in advice[1]["advice"]

    def test_accepts_plain_dict_from_state(self):
        advice = gaps.gap_advice({"ratio": 0.0,
                                  "gaps": [{"type": "scene", "value": "球场"}]})
        assert advice == [{"type": "scene", "value": "球场", "label": TYPE_LABELS["scene"],
                           "advice": gaps.ADVICE_BY_TYPE["scene"]}]

    def test_preserves_gap_order(self):
        coverage = Coverage(ratio=0.0, covered=[], gaps=[
            Element(type="topic", value="羽毛球"),
            Element(type="audience", value="打工人"),
        ])
        assert [item["value"] for item in gaps.gap_advice(coverage)] == ["羽毛球", "打工人"]

    def test_without_gaps_returns_empty(self):
        assert gaps.gap_advice(Coverage(ratio=1.0, covered=[], gaps=[])) == []
        assert gaps.gap_advice({}) == []

    def test_unknown_input_type_returns_empty(self):
        assert gaps.gap_advice(None) == []
        assert gaps.gap_advice("不是覆盖度") == []

    @pytest.mark.parametrize("element_type", ELEMENT_TYPES)
    def test_every_type_has_actionable_advice(self, element_type):
        advice = gaps.gap_advice(Coverage(gaps=[Element(type=element_type, value="x")]))
        assert advice[0]["advice"] == gaps.ADVICE_BY_TYPE[element_type]
        assert len(advice[0]["advice"]) >= 8

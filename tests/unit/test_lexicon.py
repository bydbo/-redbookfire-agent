"""词典工具单测：命中、归一化、要素组装与关键词抽取。"""

from __future__ import annotations

from xhs_agent.tools import lexicon

UNKNOWN_TEXT = "zzz 无中生有的表达"


class TestDetect:
    def test_hits_known_topic(self):
        assert ("topic", "羽毛球", "羽毛球") in lexicon.detect("周末去球场打羽毛球")

    def test_hits_person_ip(self):
        types = {element_type for element_type, _value, _surface in lexicon.detect("某明星被拍到")}
        assert "ip" in types

    def test_fullwidth_and_case_are_normalized(self):
        hits = lexicon.detect("ＯＯＴＤ 穿搭")
        assert any(value == "穿搭" for _type, value, _surface in hits)

    def test_blank_text_returns_empty(self):
        assert lexicon.detect("") == []
        assert lexicon.detect("   ") == []

    def test_unknown_text_returns_empty(self):
        assert lexicon.detect(UNKNOWN_TEXT) == []

    def test_detect_is_stable(self):
        text = "明星打羽毛球，反差感拉满"
        assert lexicon.detect(text) == lexicon.detect(text)


class TestElementsFromText:
    def test_groups_by_type(self):
        types = {e.type for e in lexicon.elements_from_text("明星打羽毛球")}
        assert {"topic", "ip"} <= types

    def test_max_per_type_caps_each_bucket(self):
        text = "羽毛球 篮球 跑步 游泳"
        capped = lexicon.elements_from_text(text, max_per_type=1)
        assert len([e for e in capped if e.type == "topic"]) == 1

    def test_weight_and_confidence_decay_with_index(self):
        topic = [e for e in lexicon.elements_from_text("羽毛球 篮球 跑步", max_per_type=3)
                 if e.type == "topic"]
        assert len(topic) == 3
        assert topic[0].weight > topic[1].weight > topic[2].weight
        assert topic[0].confidence > topic[1].confidence

    def test_generic_fallback_keeps_original_fragment(self):
        elements = lexicon.elements_from_text(UNKNOWN_TEXT)
        assert len(elements) == 1
        assert elements[0].type == "topic"
        assert elements[0].evidence == "原文片段"

    def test_generic_fallback_can_be_disabled(self):
        assert lexicon.elements_from_text(UNKNOWN_TEXT, generic_fallback=False) == []


class TestElementsFromTags:
    def test_lexicon_hit_wins(self):
        elements = lexicon.elements_from_tags(["羽毛球"])
        assert [(e.type, e.value) for e in elements] == [("topic", "羽毛球")]
        assert elements[0].confidence == 0.85
        assert elements[0].evidence == "标签：羽毛球"

    def test_title_and_description_are_scanned(self):
        values = {e.value for e in lexicon.elements_from_tags([], title="晨练",
                                                             description="球场挥拍")}
        assert {"羽毛球", "球场"} <= values

    def test_custom_tag_is_kept_as_topic(self):
        tag = "我的奇怪标签词"
        assert lexicon.detect(tag) == []  # 前提：词典确实不认识这个词
        elements = lexicon.elements_from_tags([tag])
        assert [(e.type, e.value, e.evidence) for e in elements] == \
            [("topic", tag, "自定义标签")]

    def test_too_short_custom_tag_is_dropped(self):
        assert lexicon.elements_from_tags(["猫"], description="") == []

    def test_result_is_capped_at_sixteen(self):
        tags = [f"自定义标签{i}" for i in range(20)]
        assert len(lexicon.elements_from_tags(tags)) == 16


class TestKeywords:
    def test_lexicon_values_come_first(self):
        assert lexicon.keywords_from_text("打羽毛球")[0] == "羽毛球"

    def test_dedupes_normalized_duplicates(self):
        keywords = lexicon.keywords_from_text("羽毛球,羽毛球，羽毛球")
        assert keywords.count("羽毛球") == 1

    def test_respects_limit(self):
        text = "羽毛球 篮球 跑步 游泳 露营 骑行 滑雪 咖啡 猫咪 狗狗 摄影 手作 游戏"
        assert len(lexicon.keywords_from_text(text, limit=3)) == 3

    def test_blank_text_returns_empty(self):
        assert lexicon.keywords_from_text("") == []

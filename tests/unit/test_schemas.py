"""schemas.py 的结构契约测试（S1.1）。

只覆盖内存契约本身：字段归一化、`to_dict()` / `from_dict()` 往返、
以及 `MatchCandidate.reasons` 的非空硬约束。
`tools/` 的行为回归属于 S1.4，不在这里重复。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from xhs_agent.schemas import (
    DEFAULT_TYPE_WEIGHTS,
    ELEMENT_TYPES,
    TYPE_LABELS,
    Coverage,
    Draft,
    Element,
    ElementHit,
    HotspotClue,
    MatchCandidate,
    Material,
    Mechanism,
    SchemaError,
)


def _material(**overrides) -> Material:
    base = {"id": "m_1", "path": "D:/materials/球场热身.mp4"}
    base.update(overrides)
    return Material(**base)


class TestElement:
    def test_unknown_type_falls_back_to_topic(self):
        assert Element(type="明星", value="顶流").type == "topic"

    def test_type_is_trimmed_and_case_insensitive(self):
        assert Element(type=" IP ", value="顶流").type == "ip"

    def test_empty_type_falls_back_to_topic(self):
        assert Element(type=None, value="顶流").type == "topic"

    def test_weight_and_confidence_are_clipped(self):
        element = Element(type="topic", value="羽毛球", weight=5, confidence=-3)
        assert element.weight == 1.0
        assert element.confidence == 0.0

    def test_unparsable_numbers_fall_back_to_defaults(self):
        element = Element(type="topic", value="羽毛球", weight="abc", confidence=None)
        assert element.weight == 0.6
        assert element.confidence == 0.7

    def test_value_and_evidence_are_stripped(self):
        element = Element(type="topic", value="  羽毛球  ", evidence=None)
        assert element.value == "羽毛球"
        assert element.evidence == ""

    def test_label_comes_from_type_table(self):
        assert Element(type="ip", value="顶流").label == TYPE_LABELS["ip"]

    def test_score_weight_multiplies_weight_and_confidence(self):
        element = Element(type="ip", value="顶流", weight=0.5, confidence=0.5)
        assert element.score_weight == pytest.approx(0.25)

    def test_score_weight_has_floor(self):
        element = Element(type="sound", value="BGM", weight=0.01, confidence=0.01)
        assert element.score_weight == pytest.approx(0.05)

    def test_to_dict_shape_is_stable(self):
        element = Element(type="topic", value="羽毛球", weight=0.123456, confidence=0.5)
        assert element.to_dict() == {
            "type": "topic",
            "value": "羽毛球",
            "weight": 0.123,
            "confidence": 0.5,
            "evidence": "",
        }

    def test_from_dict_accepts_plain_string(self):
        element = Element.from_dict("羽毛球")
        assert (element.type, element.value) == ("topic", "羽毛球")

    @pytest.mark.parametrize("payload", [
        {"type": "topic"},
        {"type": "topic", "value": ""},
        ["topic"],
        42,
    ])
    def test_from_dict_rejects_incomplete_payload(self, payload):
        with pytest.raises(SchemaError):
            Element.from_dict(payload)


class TestMechanism:
    def test_from_dict_accepts_plain_string(self):
        mechanism = Mechanism.from_dict("反差")
        assert (mechanism.name, mechanism.explain) == ("反差", "")

    def test_from_dict_accepts_alias_keys(self):
        mechanism = Mechanism.from_dict({"title": "参与门槛低", "why": "人人能拍"})
        assert (mechanism.name, mechanism.explain) == ("参与门槛低", "人人能拍")

    def test_from_dict_rejects_missing_name(self):
        with pytest.raises(SchemaError):
            Mechanism.from_dict({"explain": "没有名字"})

    def test_from_dict_rejects_non_object(self):
        with pytest.raises(SchemaError):
            Mechanism.from_dict(42)


class TestHotspotClue:
    def test_from_dict_builds_key_and_keywords(self):
        clue = HotspotClue.from_dict({
            "hotspot_raw": "某顶流明星打羽毛球被拍",
            "elements": [{"type": "topic", "value": "羽毛球"},
                         {"type": "ip", "value": "顶流明星"}],
        })
        assert clue.hotspot_key
        assert clue.match_keywords == ["羽毛球", "顶流明星"]
        assert clue.element_values == ["羽毛球", "顶流明星"]

    def test_from_dict_dedupes_and_caps_keywords(self):
        keywords = [f"词{i}" for i in range(30)] + ["词0", "词0"]
        clue = HotspotClue.from_dict({
            "hotspot_raw": "热点",
            "elements": [{"type": "topic", "value": "羽毛球"}],
            "match_keywords": keywords,
        })
        assert len(clue.match_keywords) == 20
        assert len(set(clue.match_keywords)) == 20
        assert clue.match_keywords[0] == "词0"

    def test_from_dict_reuses_hotspot_key_when_provided(self):
        clue = HotspotClue.from_dict({
            "hotspot_raw": "热点",
            "hotspot_key": "自定义key",
            "elements": [{"type": "topic", "value": "羽毛球"}],
        })
        assert clue.hotspot_key == "自定义key"

    def test_direct_construction_fills_hotspot_key(self):
        clue = HotspotClue(hotspot_raw="某顶流明星打羽毛球")
        assert clue.hotspot_key

    def test_mechanisms_feed_why_it_works(self):
        clue = HotspotClue.from_dict({
            "hotspot_raw": "热点",
            "elements": [{"type": "topic", "value": "羽毛球"}],
            "mechanisms": [{"name": "反差", "explain": "身份反差"}],
        })
        assert clue.why_it_works == ["反差：身份反差"]

    def test_audience_string_is_wrapped(self):
        clue = HotspotClue.from_dict({
            "hotspot_raw": "热点",
            "elements": [{"type": "topic", "value": "羽毛球"}],
            "audience": "打工人",
        })
        assert clue.audience == {"core": "打工人"}

    def test_from_dict_to_dict_round_trip(self):
        payload = {
            "hotspot_raw": "某顶流明星打羽毛球被拍",
            "why_it_works": ["反差：身份反差"],
            "mechanisms": [{"name": "反差", "explain": "身份反差"}],
            "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.8,
                          "confidence": 0.9, "evidence": "热搜标题"}],
            "match_keywords": ["羽毛球"],
            "audience": {"core": "打工人"},
            "borrow_angles": ["同款球场热场"],
            "risk_notes": ["别用明星肖像"],
            "hotspot_key": "hotspot-key",
        }
        clue = HotspotClue.from_dict(payload, provider="offline", model="rule-based")
        # provider / model / created_at 由调用方（模型调用层）注入，不从 data 里读，这是既有口径。
        expected = dict(payload, provider="offline", model="rule-based",
                        created_at=clue.created_at)
        assert clue.to_dict() == expected

    def test_from_dict_takes_provider_and_model_from_caller(self):
        clue = HotspotClue.from_dict({
            "hotspot_raw": "热点",
            "elements": [{"type": "topic", "value": "羽毛球"}],
            "provider": "被忽略的写法",
            "model": "被忽略的写法",
        }, provider="deepseek", model="deepseek-flash")
        assert (clue.provider, clue.model) == ("deepseek", "deepseek-flash")

    def test_from_dict_ignores_unknown_keys(self):
        clue = HotspotClue.from_dict({
            "hotspot_raw": "热点",
            "elements": [{"type": "topic", "value": "羽毛球"}],
            "unexpected_field": "多余字段",
        })
        assert "unexpected_field" not in clue.to_dict()

    @pytest.mark.parametrize("payload", [
        {"elements": [{"type": "topic", "value": "羽毛球"}]},
        {"hotspot_raw": "热点"},
        {"hotspot_raw": "热点", "elements": []},
        "热点",
    ])
    def test_from_dict_rejects_incomplete_payload(self, payload):
        with pytest.raises(SchemaError):
            HotspotClue.from_dict(payload)


class TestMaterial:
    def test_from_dict_to_dict_round_trip(self):
        payload = {
            "id": "m_1",
            "path": "D:/materials/球场热身.mp4",
            "type": "video",
            "title": "球场热身",
            "description": "球场 挥拍",
            "tags": ["羽毛球", "挥拍"],
            "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.8,
                          "confidence": 0.9, "evidence": "文件名"}],
            "duration_s": 12.3456,
            "width": 1080,
            "height": 1920,
            "has_audio": True,
            "size_bytes": 1024,
            "mtime": 1700000000.0,
            "source": "sidecar",
            "keyframes": ["kf/1.jpg"],
            "indexed_at": "2026-09-30T12:00:00+08:00",
            "fingerprint": "1700000000-1024",
        }
        material = Material.from_dict(payload)
        expected = dict(payload, duration_s=12.35)
        assert material.to_dict() == expected
        json.dumps(material.to_dict(), ensure_ascii=False)

    def test_from_dict_tolerates_missing_fields(self):
        material = Material.from_dict({})
        assert material.id == ""
        assert material.path == ""
        assert material.type == "video"
        assert material.source == "filename"
        assert material.tags == []
        assert material.elements == []
        assert material.fingerprint == ""

    def test_quality_score_rewards_vertical_hd_video(self):
        material = _material(type="video", width=1080, height=1920, duration_s=15)
        assert material.is_vertical is True
        assert material.quality_score == pytest.approx(1.0)

    def test_quality_score_without_dimensions(self):
        assert _material().quality_score == pytest.approx(0.4)

    def test_aspect_ratio_and_orientation(self):
        landscape = _material(width=1920, height=1080)
        assert landscape.aspect_ratio == pytest.approx(1.7778, rel=1e-3)
        assert landscape.is_vertical is False

    def test_make_id_is_stable_and_prefixed(self):
        assert Material.make_id("a/b.mp4") == Material.make_id("a/b.mp4")
        assert Material.make_id("a/b.mp4").startswith("m_")


class TestMatchCandidateReasons:
    def test_final_construction_requires_reasons(self):
        with pytest.raises(ValidationError):
            MatchCandidate(material_id="m_1", material=_material())

    def test_model_validate_requires_reasons(self):
        payload = {"material_id": "m_1", "score": 0.5, "rank": 1,
                   "material": _material().to_dict(), "reasons": []}
        with pytest.raises(ValidationError):
            MatchCandidate.model_validate(payload)

    def test_final_construction_with_reasons_is_allowed(self):
        candidate = MatchCandidate(material_id="m_1", material=_material(),
                                   reasons=["命中主题「羽毛球」"])
        assert candidate.reasons == ["命中主题「羽毛球」"]

    def test_draft_allows_empty_reasons(self):
        candidate = MatchCandidate.draft(material_id="m_1", material=_material(),
                                         score=0.5, rank=1)
        assert candidate.reasons == []
        assert candidate.rank == 1

    def test_finalize_blocks_candidate_without_reasons(self):
        with pytest.raises(SchemaError):
            MatchCandidate.draft(material_id="m_1", material=_material()).finalize()

    def test_finalize_passes_after_reasons_filled(self):
        candidate = MatchCandidate.draft(material_id="m_1", material=_material(), rank=1)
        candidate.reasons = ["命中主题「羽毛球」"]
        candidate.usage = "放开头 3 秒"
        assert candidate.finalize() is candidate
        payload = candidate.to_dict()
        assert payload["reasons"] == ["命中主题「羽毛球」"]
        assert payload["usage"] == "放开头 3 秒"

    def test_draft_returns_match_candidate_instance(self):
        assert isinstance(MatchCandidate.draft(material_id="m_1", material=_material()),
                          MatchCandidate)


class TestNestedSerialization:
    def test_candidate_to_dict_nests_material_and_hits(self):
        material = _material(elements=[Element(type="topic", value="羽毛球")])
        candidate = MatchCandidate(
            material_id=material.id,
            material=material,
            score=0.98765,
            rank=2,
            hits=[ElementHit(element_type="topic", clue_value="羽毛球",
                             hit_value="羽毛球", similarity=0.98765, contribution=0.5)],
            missing=[Element(type="ip", value="顶流明星")],
            reasons=["命中主题「羽毛球」"],
            usage="放开头 3 秒",
        )
        payload = candidate.to_dict()
        assert payload["material_id"] == material.id
        assert payload["score"] == 0.988
        assert payload["rank"] == 2
        assert payload["hits"][0]["similarity"] == 0.988
        assert payload["hits"][0]["contribution"] == 0.5
        assert [e["value"] for e in payload["missing"]] == ["顶流明星"]
        assert payload["material"]["elements"][0]["value"] == "羽毛球"

    def test_coverage_to_dict_shape(self):
        coverage = Coverage(covered=[Element(type="topic", value="羽毛球")],
                            gaps=[Element(type="ip", value="顶流明星")],
                            ratio=0.6666)
        payload = coverage.to_dict()
        assert payload["ratio"] == 0.667
        assert [e["value"] for e in payload["covered"]] == ["羽毛球"]
        assert [e["value"] for e in payload["gaps"]] == ["顶流明星"]


class TestDraft:
    def test_from_dict_normalizes_titles_and_tags(self):
        draft = Draft.from_dict({
            "body": "正文",
            "titles": [{"text": "标题一"}, "标题二", {"text": ""}],
            "tags": ["羽毛球", "#挥拍"],
        })
        assert draft.titles == [{"text": "标题一", "style": "未标注"},
                                {"text": "标题二", "style": "未标注"}]
        assert draft.tags == ["#羽毛球", "#挥拍"]

    def test_titles_and_tags_are_capped(self):
        draft = Draft.from_dict({
            "body": "正文",
            "titles": [f"标题{i}" for i in range(8)],
            "tags": [f"tag{i}" for i in range(20)],
        })
        assert len(draft.titles) == 5
        assert len(draft.tags) == 12

    def test_from_dict_accepts_content_alias(self):
        draft = Draft.from_dict({"content": "正文"})
        assert draft.body == "正文"

    def test_from_dict_rejects_missing_body(self):
        with pytest.raises(SchemaError):
            Draft.from_dict({"titles": ["标题"]})

    def test_from_dict_rejects_non_object(self):
        with pytest.raises(SchemaError):
            Draft.from_dict("正文")

    def test_to_dict_carries_computed_title_warnings(self):
        draft = Draft.from_dict({"body": "正文", "titles": ["超长标题" * 6]})
        payload = draft.to_dict()
        assert len(payload["title_warnings"]) == 1
        assert set(payload) >= {"material_id", "hotspot_key", "titles", "body", "tags",
                                "cover_text", "first_3s", "shot_list", "compliance_notes",
                                "provider", "model", "created_at", "title_warnings"}

    def test_short_title_produces_no_warning(self):
        draft = Draft.from_dict({"body": "正文", "titles": ["二十字以内的标题"]})
        assert draft.title_warnings() == []

    def test_to_markdown_covers_required_sections(self):
        draft = Draft.from_dict({
            "body": "正文",
            "titles": ["标题"],
            "cover_text": "封面",
            "first_3s": "开头",
            "shot_list": ["先拍球场"],
            "compliance_notes": ["别用明星肖像"],
        })
        text = draft.to_markdown()
        for section in ("### 标题备选", "### 封面文字", "### 开头 3 秒", "### 正文",
                        "### 话题标签", "### 剪辑顺序建议", "### 合规提醒"):
            assert section in text

    def test_title_limit_is_class_level(self):
        assert Draft.XHS_TITLE_LIMIT == 20


class TestContractConstants:
    def test_element_types_match_contract_enum(self):
        assert ELEMENT_TYPES == ("ip", "topic", "scene", "visual", "emotion",
                                 "sound", "conflict", "format", "audience")

    def test_labels_cover_every_element_type(self):
        assert set(TYPE_LABELS) == set(ELEMENT_TYPES)

    def test_weights_cover_every_element_type(self):
        assert set(DEFAULT_TYPE_WEIGHTS) == set(ELEMENT_TYPES)

    def test_schema_error_is_value_error(self):
        assert issubclass(SchemaError, ValueError)

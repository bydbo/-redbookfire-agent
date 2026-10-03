"""写回仓储层的离线单测（S3.4a）：候选 → `run_matches` 字段的纯映射。

不连库：只覆盖 `services.runs` 里可离线验证的那部分；真正的写库由
`tests/integration/test_run_writeback.py` 在真容器上验证。
"""

from __future__ import annotations

import pytest

from xhs_agent.schemas import SchemaError
from xhs_agent.services import runs

MATERIAL_ID = "6f1e0f7a-6f6f-4f4f-8f4f-000000000001"


def candidate(**overrides):
    payload = {
        "material_id": MATERIAL_ID,
        "rank": 2,
        "score": 0.8123,
        "recall_sources": ["literal", "vector"],
        "hits": [{"type": "topic", "value": "羽毛球", "weight": 1.0, "contribution": 1.0}],
        "missing": [{"type": "sound", "value": "球拍击球声"}],
        "reasons": ["命中主题「羽毛球」"],
        "usage": "放开头 3 秒",
    }
    payload.update(overrides)
    return payload


class TestMatchRowValues:
    def test_maps_every_column(self):
        values = runs.match_row_values(candidate())
        assert values["rank"] == 2
        assert values["score"] == pytest.approx(0.8123)
        assert values["recall_sources"] == ["literal", "vector"]   # 顺序原样保留（归一化在 Pydantic 层）
        assert values["hits"][0]["value"] == "羽毛球"
        assert values["missing"][0]["type"] == "sound"
        assert values["reasons"] == ["命中主题「羽毛球」"]
        assert values["usage"] == "放开头 3 秒"

    def test_does_not_mutate_the_candidate(self):
        payload = candidate()
        before = {key: list(value) if isinstance(value, list) else value
                  for key, value in payload.items()}
        runs.match_row_values(payload)
        assert payload == before

    def test_defaults_for_optional_columns(self):
        values = runs.match_row_values(candidate(hits=None, missing=None, usage=None))
        assert values["hits"] == [] and values["missing"] == [] and values["usage"] == ""

    def test_blank_reasons_are_dropped_then_rejected(self):
        with pytest.raises(SchemaError, match="缺少 reasons"):
            runs.match_row_values(candidate(reasons=["", "   "]))
        values = runs.match_row_values(candidate(reasons=["  理由  "]))
        assert values["reasons"] == ["  理由  "]   # 只判空，不改写正文

    def test_missing_reasons_key_is_rejected(self):
        payload = candidate()
        payload.pop("reasons")
        with pytest.raises(SchemaError):
            runs.match_row_values(payload)

    def test_rank_must_be_at_least_one(self):
        for bad_rank in (0, -1, None):
            with pytest.raises(SchemaError, match="rank"):
                runs.match_row_values(candidate(rank=bad_rank))


class TestCandidateMaterialId:
    def test_accepts_material_id_or_nested_material(self):
        assert str(runs._candidate_material_id(candidate())) == MATERIAL_ID
        nested = candidate(material_id="", material={"id": MATERIAL_ID})
        assert str(runs._candidate_material_id(nested)) == MATERIAL_ID

    def test_rejects_unparseable_id(self):
        with pytest.raises(SchemaError, match="material_id"):
            runs._candidate_material_id(candidate(material_id="not-a-uuid"))
        with pytest.raises(SchemaError, match="material_id"):
            runs._candidate_material_id(candidate(material_id=""))

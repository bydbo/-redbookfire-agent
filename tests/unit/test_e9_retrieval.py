"""E9 四策略编排的纯函数部分：oracle、路径映射与通道选择。"""

from __future__ import annotations

from xhs_agent.evaluation.models import Case, Dataset, DatasetManifest, MaterialAsset
from xhs_agent.evaluation.retrieval import (
    STRATEGY_CHANNELS,
    build_oracle_clue,
    path_key_map,
)


def _case() -> Case:
    return Case.model_validate({
        "case_id": "case-a", "group_id": "g", "split": "dev", "input_type": "single_image",
        "natural_input": {"text": "周末早餐", "images": [
            {"material_key": "mat-a", "sha256": "a" * 64}]},
        "expectation": {"answerable": True, "summary": "测试"},
        "oracle": {"elements": [{"type": "topic", "value": "早餐"}],
                   "match_keywords": ["早餐"]},
        "scenario_tags": ["natural_naming"], "notes": "",
    })


def test_oracle_clue_uses_annotations_not_model() -> None:
    clue = build_oracle_clue(_case())
    assert clue.hotspot_raw == "周末早餐"
    assert [element.value for element in clue.elements] == ["早餐"]
    assert clue.match_keywords == ["早餐"]


def test_path_map_normalizes_separators() -> None:
    dataset = Dataset(
        manifest=DatasetManifest(version="v", protocol_version="1",
                                 dataset_kind="engineering_fake", status="draft"),
        materials=[MaterialAsset(
            material_key="mat-a", relative_path="assets\\a.jpg", sha256="a" * 64,
            media_type="image", source="s", license="CC0",
            model_transfer_allowed=True)],
        cases=[], qrels=[], reviews=[], e2e_tasks=[],
    )
    assert path_key_map(dataset) == {"assets/a.jpg": "mat-a"}


def test_strategy_channels_are_explicit() -> None:
    assert STRATEGY_CHANNELS["keyword"] is None
    assert STRATEGY_CHANNELS["trgm_only"] == ("literal",)
    assert STRATEGY_CHANNELS["vector_only"] == ("vector",)
    assert STRATEGY_CHANNELS["hybrid"] == ("literal", "vector")

"""对话用例集的结构校验（S7.12）：`evals/chat/cases.json`。

这套用例**不属于** evals v1 的冻结资产（那是检索链路的 20 组用例）：它自带 `version`
（当前 `chat-v1`），改动只升这个版本号。这里只钉结构与口径，不跑真模型——
真模型回归在 `scripts/eval_chat_tools.py`（报告落 `evals/reports/`）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xhs_agent.services.chat_tools import tool_names
from xhs_agent.tools import prompt as prompt_tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = PROJECT_ROOT / "evals" / "chat" / "cases.json"
SECTIONS = ("selection", "boundary", "multiturn", "revise")


@pytest.fixture(scope="module")
def cases() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


class TestShape:
    def test_has_four_sections_and_a_version(self, cases):
        assert cases["version"].startswith("chat-v")
        for section in SECTIONS:
            assert isinstance(cases.get(section), list) and cases[section], section

    def test_names_are_unique_across_sections(self, cases):
        names = [item["name"] for section in SECTIONS for item in cases[section]]
        assert len(set(names)) == len(names)

    def test_counts_match_the_story(self, cases):
        """S7.12 的口径：越界 8–10 条、多轮 5 条、工具选择 10 条、改写 3 条。"""
        assert 8 <= len(cases["boundary"]) <= 10
        assert len(cases["multiturn"]) == 5
        assert len(cases["selection"]) == 10
        assert len(cases["revise"]) == 3


class TestSelection:
    def test_expectations_only_reference_registered_tools(self, cases):
        known = tool_names()
        for item in cases["selection"]:
            assert set(item["expect"]) <= known, item["name"]


class TestBoundary:
    def test_categories_come_from_the_capability_catalog(self, cases):
        ids = {item["id"] for item in prompt_tool.load_capabilities()["categories"]}
        for item in cases["boundary"]:
            assert item["category"] in ids, item["name"]

    def test_covers_both_guidable_and_red_line(self, cases):
        categories = [item["category"] for item in cases["boundary"]]
        assert categories.count("out_of_scope_guidable") >= 3
        assert categories.count("compliance_red_line") >= 3

    def test_every_case_has_a_replacement_path_or_refusal_marker(self, cases):
        """没有断言规则的用例等于没测：每条都必须给 `should_mention_any`。"""
        for item in cases["boundary"]:
            assert item["should_mention_any"], item["name"]

    def test_red_line_cases_forbid_the_boundary_entities(self, cases):
        """红线用例至少一半要带禁词表（"不编造"只能这么机读地判）。"""
        red = [item for item in cases["boundary"]
               if item["category"] == "compliance_red_line"]
        with_words = [item for item in red if item["must_not_contain"]]
        assert len(with_words) >= len(red) // 2

    def test_boundary_cases_never_allow_write_tools(self, cases):
        """越界用例里不许出现会跑完整分析的工具（那是"没拦住"的信号）。"""
        for item in cases["boundary"]:
            assert set(item["expect_tools"]) <= tool_names(), item["name"]
            assert "run_hotspot_analysis" not in item["expect_tools"], item["name"]


class TestMultiTurn:
    def test_each_turn_declares_its_tool_whitelist(self, cases):
        for item in cases["multiturn"]:
            assert len(item["turns"]) == len(item["expect_tools"]), item["name"]
            for group in item["expect_tools"]:
                assert set(group) <= tool_names(), item["name"]

    def test_long_history_case_asserts_the_budget(self, cases):
        seeded = [item for item in cases["multiturn"] if item.get("seed_history")]
        assert seeded, "至少要有一条长历史用例来验预算裁剪"
        assert any(item.get("assert_budget") for item in seeded)


class TestRevise:
    def test_has_both_refusal_and_normal_cases(self, cases):
        flags = [item["expect_refused"] for item in cases["revise"]]
        assert True in flags and False in flags

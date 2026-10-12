"""七条 prompt 资产的完整性守卫（S3.1 补完三条 agent 正文后变为硬约束；S7.3 加对话 supervisor，S7.8 加改写）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from xhs_agent.services import chat_tools
from xhs_agent.tools import prompt as prompt_tool

AGENT_TASKS = ("hotspot_clue", "material_select", "copy_draft", "chat_supervisor",
               "copy_revise")
ALL_TASKS = (*AGENT_TASKS, "material_tagging", "image_hotspot_clue")
PLACEHOLDER_MARKERS = ("待 S3.1 填写", "待填写")
# 版本号只随内容变更递增（prompt 契约 §三 / §六）——钉住当前值，顺手改正文却忘了升版本会被挡下
EXPECTED_VERSIONS = {"hotspot_clue": 1, "material_select": 1, "copy_draft": 1,
                     "chat_supervisor": 2, "copy_revise": 1}
CONTRACT_PATH = (Path(__file__).resolve().parents[2]
                 / "docs" / "contracts" / "prompt契约.md")


class TestPromptAssets:
    @pytest.mark.parametrize("task_id", ALL_TASKS)
    def test_parses_and_has_five_non_empty_sections(self, task_id):
        loaded = prompt_tool.load(task_id)
        assert set(loaded.sections) == {"system", "task", "constraints", "examples",
                                        "output_schema"}
        for key, body in loaded.sections.items():
            assert body.strip(), f"{task_id} 的 {key} 段是空的"

    @pytest.mark.parametrize("task_id", AGENT_TASKS)
    def test_no_pending_placeholder_left(self, task_id):
        """S3.1 之后这三条 prompt 的正文必须写完，不能再留"待填写"提示。"""
        loaded = prompt_tool.load(task_id)
        text = "\n".join(loaded.sections.values())
        for marker in PLACEHOLDER_MARKERS:
            assert marker not in text, f"{task_id} 还留着「{marker}」"

    @pytest.mark.parametrize("task_id", AGENT_TASKS)
    def test_examples_and_output_schema_are_delivered(self, task_id):
        loaded = prompt_tool.load(task_id)
        assert len(loaded.sections["examples"]) >= 40
        assert len(loaded.sections["output_schema"]) >= 40

    @pytest.mark.parametrize("task_id", AGENT_TASKS)
    def test_agent_prompt_versions_are_pinned(self, task_id):
        """版本号钉住：改正文必须同时升版本，升版本就必须跑回归（prompt 契约 §六）。"""
        assert prompt_tool.load(task_id).version == EXPECTED_VERSIONS[task_id]

    def test_constraints_keep_the_four_hard_rules(self):
        for task_id in ALL_TASKS:
            constraints = prompt_tool.load(task_id).sections["constraints"]
            assert "不确定的信息，不能写成事实" in constraints

    def test_task_sections_keep_declared_placeholders(self):
        for task_id in ALL_TASKS:
            loaded = prompt_tool.load(task_id)
            assert set(loaded.requires) == loaded.placeholders()


class TestToolCatalog:
    """prompt 契约 §一 的对话工具清单必须与代码里的清单一一对上（S7.7 起）。"""

    def test_contract_lists_every_registered_tool(self):
        section = CONTRACT_PATH.read_text(encoding="utf-8").split("**对话工具清单", 1)[1]
        missing = sorted(name for name in chat_tools.tool_names()
                         if f"`{name}`" not in section)
        assert not missing, f"prompt 契约的对话工具清单漏了：{missing}"

    def test_contract_says_where_the_schema_lives(self):
        """契约只登记"有什么"，schema 的唯一来源必须是代码——这句不能丢。"""
        section = CONTRACT_PATH.read_text(encoding="utf-8").split("**对话工具清单", 1)[1]
        assert "services/chat_tools.py" in section and "不复制" in section

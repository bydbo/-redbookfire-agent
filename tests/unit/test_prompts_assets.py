"""六条 prompt 资产的完整性守卫（S3.1 补完三条 agent 正文后变为硬约束；S7.3 加对话 supervisor）。"""

from __future__ import annotations

import pytest

from xhs_agent.tools import prompt as prompt_tool

AGENT_TASKS = ("hotspot_clue", "material_select", "copy_draft", "chat_supervisor")
ALL_TASKS = (*AGENT_TASKS, "material_tagging", "image_hotspot_clue")
PLACEHOLDER_MARKERS = ("待 S3.1 填写", "待填写")


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
    def test_agent_prompts_stay_on_v1(self, task_id):
        """v1 骨架从未被任何运行消费，S3.1 补正文视为 v1 的完成（prompt 契约 §三）。"""
        assert prompt_tool.load(task_id).version == 1

    def test_constraints_keep_the_four_hard_rules(self):
        for task_id in ALL_TASKS:
            constraints = prompt_tool.load(task_id).sections["constraints"]
            assert "不确定的信息，不能写成事实" in constraints

    def test_task_sections_keep_declared_placeholders(self):
        for task_id in ALL_TASKS:
            loaded = prompt_tool.load(task_id)
            assert set(loaded.requires) == loaded.placeholders()

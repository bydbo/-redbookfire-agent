"""prompt 资产与渲染器单测（S3.0；契约 §二 / §三 / §四）。

覆盖加载、解析、组装与全部失败行为；不调用任何模型、不读 config/.env。
"""

from __future__ import annotations

import pytest

from xhs_agent.tools import prompt as prompt_tools

VALID = """# demo · 示例任务

<!-- prompt-version: v2 -->
<!-- task-id: demo -->
<!-- requires: name -->

## 01 System

角色。

## 02 Task

你好，{{ name }}。

## 03 Constraints

边界。

## 04 Examples

示例。

## 05 Output Schema

字段。
"""


def _drop_section(text: str, title: str) -> str:
    """按段标题丢弃一整段，避免依赖具体换行符。"""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = line.strip() == f"## {title}"
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


class TestAssets:
    def test_available_tasks_lists_five_assets(self):
        assert prompt_tools.available_tasks() == (
            "copy_draft", "hotspot_clue", "image_hotspot_clue", "material_select",
            "material_tagging")

    def test_image_hotspot_clue_has_no_placeholders(self):
        """图片热点拆解没有文本变量（图随消息附上），`requires` 因此是空的。"""
        loaded = prompt_tools.load("image_hotspot_clue")
        assert loaded.version == 1
        assert loaded.requires == ()
        assert loaded.placeholders() == set()

    def test_material_tagging_version_and_requires(self):
        loaded = prompt_tools.load("material_tagging")
        assert loaded.version == 1
        assert loaded.requires == ("file_name_hint",)
        assert loaded.placeholders() == {"file_name_hint"}

    @pytest.mark.parametrize("task_id", ["hotspot_clue", "material_select", "copy_draft"])
    def test_pending_tasks_have_all_five_sections(self, task_id: str):
        loaded = prompt_tools.load(task_id)
        assert set(loaded.sections) == set(prompt_tools.SECTION_KEYS)
        assert all(loaded.sections[key].strip() for key in prompt_tools.SECTION_KEYS)
        assert set(loaded.requires) == loaded.placeholders()
        constraints = loaded.sections["constraints"]
        assert "总原则" in constraints
        assert "不确定的信息，不能写成事实" in constraints

    def test_unknown_task_id_lists_available_tasks(self):
        with pytest.raises(prompt_tools.PromptError) as excinfo:
            prompt_tools.load("nope")
        assert "nope" in str(excinfo.value)
        assert "material_tagging" in str(excinfo.value)


class TestParse:
    def test_parses_version_and_task_id(self):
        parsed = prompt_tools.parse(VALID)
        assert (parsed.task_id, parsed.version) == ("demo", 2)

    def test_missing_section_raises(self):
        with pytest.raises(prompt_tools.PromptError) as excinfo:
            prompt_tools.parse(_drop_section(VALID, "03 Constraints"))
        assert "03 Constraints" in str(excinfo.value)

    def test_wrong_section_title_raises(self):
        with pytest.raises(prompt_tools.PromptError) as excinfo:
            prompt_tools.parse(VALID.replace("## 02 Task", "## 02 任务"))
        assert "02 Task" in str(excinfo.value)

    def test_missing_version_raises(self):
        with pytest.raises(prompt_tools.PromptError):
            prompt_tools.parse(VALID.replace("<!-- prompt-version: v2 -->", ""))

    def test_requires_mismatch_raises(self):
        with pytest.raises(prompt_tools.PromptError) as excinfo:
            prompt_tools.parse(VALID.replace("<!-- requires: name -->",
                                             "<!-- requires: name, other -->"))
        assert "other" in str(excinfo.value)

    def test_task_id_mismatch_raises(self):
        with pytest.raises(prompt_tools.PromptError):
            prompt_tools.parse(VALID, task_id="other")


class TestRender:
    def test_system_is_01_plus_03_and_user_is_02_04_05(self):
        rendered = prompt_tools.parse(VALID).render(name="世界")
        assert "角色。" in rendered.system and "边界。" in rendered.system
        assert "你好，世界。" in rendered.user
        assert "示例。" in rendered.user and "字段。" in rendered.user
        assert rendered.version == 2 and rendered.task_id == "demo"

    def test_missing_variable_reports_task_and_name(self):
        with pytest.raises(prompt_tools.PromptError) as excinfo:
            prompt_tools.parse(VALID).render()
        message = str(excinfo.value)
        assert "demo" in message and "name" in message

    def test_none_counts_as_missing(self):
        with pytest.raises(prompt_tools.PromptError):
            prompt_tools.parse(VALID).render(name=None)

    def test_extra_variables_are_ignored(self):
        rendered = prompt_tools.parse(VALID).render(name="世界", extra="无关内容")
        assert "无关内容" not in rendered.user

    def test_placeholders_outside_task_are_literal(self):
        text = VALID.replace("示例。", "示例：{{ name }}")
        rendered = prompt_tools.parse(text).render(name="世界")
        assert "{{ name }}" in rendered.user

    def test_render_by_task_id_fills_hint(self):
        rendered = prompt_tools.render("material_tagging", file_name_hint="做菜-周末")
        assert "做菜-周末" in rendered.user
        assert "{{" not in rendered.user and "{{" not in rendered.system

"""十条 prompt 资产的完整性、版本与信任边界守卫。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xhs_agent.services import chat_tools
from xhs_agent.tools import prompt as prompt_tool

AGENT_TASKS = ("hotspot_clue", "material_select", "copy_draft", "chat_supervisor",
               "copy_revise", "image_hotspot_clue_multi")
ALL_TASKS = (*AGENT_TASKS, "material_tagging", "image_hotspot_clue",
             "context_extract", "context_recover")
PLACEHOLDER_MARKERS = ("待 S3.1 填写", "待填写")
# 版本号只随内容变更递增（prompt 契约 §三 / §六）——钉住当前值，顺手改正文却忘了升版本会被挡下
EXPECTED_VERSIONS = {"hotspot_clue": 2, "material_select": 2, "copy_draft": 3,
                     "chat_supervisor": 6, "copy_revise": 3,
                     "image_hotspot_clue_multi": 4, "image_hotspot_clue": 5,
                     "material_tagging": 2, "context_extract": 2, "context_recover": 3}
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

    @pytest.mark.parametrize("task_id", ALL_TASKS)
    def test_agent_prompt_versions_are_pinned(self, task_id):
        """版本号钉住：改正文必须同时升版本，升版本就必须跑回归（prompt 契约 §六）。"""
        assert prompt_tool.load(task_id).version == EXPECTED_VERSIONS[task_id]

    def test_constraints_keep_the_four_hard_rules(self):
        for task_id in (*AGENT_TASKS, "material_tagging", "image_hotspot_clue"):
            constraints = prompt_tool.load(task_id).sections["constraints"]
            assert "不确定的信息，不能写成事实" in constraints

    def test_task_sections_keep_declared_placeholders(self):
        for task_id in ALL_TASKS:
            loaded = prompt_tool.load(task_id)
            assert set(loaded.requires) == loaded.placeholders()

    @pytest.mark.parametrize("task_id", ALL_TASKS)
    def test_current_task_never_contains_static_example_inputs(self, task_id):
        loaded = prompt_tool.load(task_id)
        rendered = loaded.render(**dict.fromkeys(loaded.requires, "当前业务输入"))
        assert loaded.sections["examples"] not in rendered.user
        assert loaded.sections["examples"] in rendered.system
        assert "当前业务输入" not in rendered.system
        assert "04 Examples 仅示范输出格式" in rendered.system
        assert "不要输出、复述、翻译、编码" in rendered.system


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


class TestCapabilityCatalog:
    """机读能力清单（S7.11）：唯一来源，且必须真的被 `chat_supervisor` 用上。"""

    def test_three_categories_in_contract_order(self):
        payload = prompt_tool.load_capabilities()
        assert [item["id"] for item in payload["categories"]] == \
            list(prompt_tool.CAPABILITY_CATEGORIES)
        assert [item["title"] for item in payload["categories"]] == \
            ["能力内", "边界外可引导", "合规红线"]

    def test_red_line_refuses_and_forbids_workarounds(self):
        """红线话术必须拒绝 + 不给绕过写法（拼音/缩写/谐音都算绕过）。"""
        red = next(item for item in prompt_tool.load_capabilities()["categories"]
                   if item["id"] == "compliance_red_line")
        assert "不能做" in red["reply"]
        assert "绕过" in red["reply"]

    def test_guidable_category_offers_an_alternative(self):
        """边界外可引导：要说清"不在范围内"，也要给出替代路径。"""
        item = next(item for item in prompt_tool.load_capabilities()["categories"]
                    if item["id"] == "out_of_scope_guidable")
        assert "不在这套工具的能力范围内" in item["reply"]
        assert "我能做的是" in item["reply"]

    def test_capabilities_are_not_a_prompt_task(self):
        """它是数据资产：不进 `available_tasks()`（只扫 `.md`），否则会被当成缺五段的 prompt。"""
        assert prompt_tool.CAPABILITIES_NAME.endswith(".json")
        assert prompt_tool.CAPABILITIES_NAME[:-5] not in prompt_tool.available_tasks()

    def test_supervisor_injects_the_catalog(self):
        rendered = prompt_tool.render("chat_supervisor",
                                      capabilities=prompt_tool.render_capabilities())
        both = rendered.system + rendered.user
        for item in prompt_tool.load_capabilities()["categories"]:
            assert item["title"] in both
            assert item["reply"] in both
        # §01 System 段必须声明界限来源在清单上（契约要求的"System 段引用它"）
        assert "能力清单" in rendered.system


class TestMultiImagePrompt:
    """多图合并口径（S7.15）：一次一份、共同优先、独特降权、`evidence` 必须指图。"""

    def test_constraints_carry_the_merge_rules(self):
        constraints = prompt_tool.load("image_hotspot_clue_multi").sections["constraints"]
        assert "共同优先" in constraints
        assert "独特降权" in constraints
        assert "0.4" in constraints                      # 独特要素的权重上限
        assert "以偏概全" in constraints                  # 不许把单张的东西说成整组
        assert "3–8" in constraints
        assert "evidence" in constraints

    def test_task_asks_for_exactly_one_result(self):
        task = prompt_tool.load("image_hotspot_clue_multi").sections["task"]
        assert "一份" in task

    def test_output_schema_points_at_the_image_number(self):
        schema = prompt_tool.load("image_hotspot_clue_multi").sections["output_schema"]
        assert "图N" in schema and "evidence" in schema and "3–8" in schema

class TestSupervisorWebToolWording:
    """Task 8：v6 把网页工具写成条件式——`[web].enabled` 默认关闭，措辞必须与开关无关。"""

    WEB_TOOL_NAMES = (chat_tools.TOOL_SEARCH_HOTSPOTS, chat_tools.TOOL_FETCH_URL)
    # 提到网页工具的那一句必须自带条件词：否则关闭态下模型会去调一个清单里没有的工具
    CONDITION_MARKERS = ("如果", "若", "即便", "清单里", "本轮工具", "没有这类工具")
    _SENTENCE_RE = re.compile(r"[^。！？；]+")
    _LIST_ITEM_RE = re.compile(r"^\s*(?:[（(]?\d+[)）.、]|\d+\.|[-*])\s")

    def _text(self) -> str:
        loaded = prompt_tool.load("chat_supervisor")
        return "\n".join(loaded.sections[key] for key in prompt_tool.SECTION_KEYS)

    def _sentences(self, text: str) -> list[str]:
        """按句子切：markdown 的硬折行先接回上一行，只有列表项才算新起一句。"""
        lines: list[str] = []
        for line in text.splitlines():
            if line.strip() and lines and lines[-1] and not self._LIST_ITEM_RE.match(line):
                lines[-1] += line.strip()
            else:
                lines.append(line.strip())
        return [chunk.strip() for chunk in self._SENTENCE_RE.findall("\n".join(lines))
                if chunk.strip()]

    def test_chat_supervisor_version_is_v6(self) -> None:
        assert prompt_tool.load("chat_supervisor").version == 6

    def test_prompt_mentions_web_tools_conditionally(self) -> None:
        assert "如果本轮工具清单里" in self._text()
        for sentence in self._sentences(self._text()):
            if not any(name in sentence for name in self.WEB_TOOL_NAMES):
                continue
            assert any(marker in sentence for marker in self.CONDITION_MARKERS), \
                f"这句无条件断言了网页工具：{sentence}"

    def test_prompt_has_no_hardcoded_round_budget(self) -> None:
        constraints = prompt_tool.load("chat_supervisor").sections["constraints"]
        assert "以本轮系统说明为准" in constraints
        for hardcoded in ("默认 3 次", "默认3次", "8 次", "8次", "3 轮", "8 轮"):
            assert hardcoded not in self._text(), f"轮次预算写死了：{hardcoded}"

    def test_v6_documents_the_three_web_tool_constraints(self) -> None:
        constraints = prompt_tool.load("chat_supervisor").sections["constraints"]
        assert "候选" in constraints and "不是事实" in constraints   # 搜索只是候选，不是事实
        assert "不得自行构造 URL" in constraints                     # 不许自己拼链接
        assert "天气" in constraints                                 # 通用查询不归网页工具

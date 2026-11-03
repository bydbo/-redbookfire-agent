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


class TestCapabilityCatalogV2:
    """能力清单 v2（Task 9）：顶层 `web_clause` + 按 `[web]` 开关渲染。

    关闭态**不追加** `web_clause`（判据见 brief 修正 1：与 v1 字面比必然红，且红得合法），
    开启态把网页能力当作「叠加在能力内之上的一项额外能力」，不是第四类边界。
    """

    def test_capabilities_v2_has_web_clause(self):
        payload = prompt_tool.load_capabilities()
        assert payload["version"] == 2
        # 必须是数字：写回 "v1" 会让标题渲染成 "vv1"
        assert isinstance(payload["version"], int)
        assert not isinstance(payload["version"], bool)
        clause = payload["web_clause"]
        assert clause["judge_append"].strip()
        assert clause["reply_append"].strip()

    def test_render_web_disabled_matches_v2_without_clause(self):
        """修正 1：关闭态 == 「v2 渲染但不追加 web_clause」，逐字相等。"""
        payload = prompt_tool.load_capabilities()
        without_clause = {key: value for key, value in payload.items()
                          if key != "web_clause"}
        assert prompt_tool.render_capabilities(payload, web_enabled=False) == \
            prompt_tool.render_capabilities(without_clause, web_enabled=False)
        # 修正 2：关闭态标题保持原样（含「三类边界」）
        assert prompt_tool.render_capabilities().splitlines()[0] == "【能力清单 v2 · 三类边界】"

    def test_render_web_disabled_has_no_web_trace(self):
        text = prompt_tool.render_capabilities(web_enabled=False)
        for marker in ("网页", "搜索热点", "读取网页", "search_hotspots",
                       "fetch_url", "web_clause"):
            assert marker not in text, marker

    def test_render_web_enabled_appends_clause(self):
        clause = prompt_tool.load_capabilities()["web_clause"]
        text = prompt_tool.render_capabilities(web_enabled=True)
        assert clause["judge_append"] in text
        assert clause["reply_append"] in text
        # 修正 2：追加了网页能力，标题必须涵盖它，不能仍旧只报「三类边界」
        off_header = prompt_tool.render_capabilities().splitlines()[0]
        on_header = text.splitlines()[0]
        assert "三类边界" in off_header and "网页" not in off_header
        assert "网页" in on_header and on_header != off_header

    def test_render_web_enabled_is_not_a_fourth_category(self):
        """自检口径：三项边界编号不变，网页能力是附加项，不凭空多出一类。"""
        text = prompt_tool.render_capabilities(web_enabled=True)
        numbered = [line for line in text.splitlines() if re.match(r"^\d+\. ", line)]
        assert len(numbered) == 3

    def test_missing_search_tool_is_not_out_of_scope(self):
        """修正 3：缺一个可选工具不归边界外，否则那句被禁的话会重新获得授权。"""
        payload = prompt_tool.load_capabilities()
        by_id = {item["id"]: item for item in payload["categories"]}
        guidable = by_id["out_of_scope_guidable"]
        in_scope = by_id["in_scope"]
        assert "缺少一个可选工具" in guidable["judge"] + in_scope["judge"]
        assert "不归边界外" in guidable["judge"]
        # 新增内容：通用信息查询不属于热点搜索
        assert "通用信息查询即使能搜到，也不属于热点搜索" in guidable["judge"]
        # 适用前提必须落在 judge（判定槽），不许塞进固定话术
        assert "「帮我找热点」" in guidable["judge"]
        assert "不要套用下面的固定话术" in guidable["judge"]

    def test_guidable_reply_is_pure_user_facing_text(self):
        """固定话术只能放说给用户的原话。

        它同时是 `output_guard` 的公开话术白名单来源——一旦把内部口径写进这里，
        等于给这次泄漏开绿灯（白名单会一起放行）。
        """
        reply = next(item for item in prompt_tool.load_capabilities()["categories"]
                     if item["id"] == "out_of_scope_guidable")["reply"]
        for marker in ("〔", "judge", "判定", "不归边界外", "搜索类工具", "帮我找热点"):
            assert marker not in reply, marker
        # 原话术逐字保留（既有断言与 output_guard 白名单都依赖它）
        assert reply.startswith("这件事不在这套工具的能力范围内。")
        assert "我能做的是" in reply

    def test_render_web_enabled_without_clause_raises(self):
        """开启态但清单里没有可用的 web_clause：抛错，不静默降级成空的「判定：」。"""
        payload = {key: value for key, value in prompt_tool.load_capabilities().items()
                   if key != "web_clause"}
        with pytest.raises(prompt_tool.PromptError):
            prompt_tool.render_capabilities(payload, web_enabled=True)
        blank = dict(prompt_tool.load_capabilities())
        blank["web_clause"] = {"judge_append": "  ", "reply_append": "话术"}
        with pytest.raises(prompt_tool.PromptError):
            prompt_tool.render_capabilities(blank, web_enabled=True)
        # 关闭态不受影响：同一份没有 web_clause 的清单照常渲染
        assert "三类边界" in prompt_tool.render_capabilities(payload, web_enabled=False)

    def test_initial_messages_passes_web_enabled(self):
        from xhs_agent.agents.supervisor import initial_messages

        clause = prompt_tool.load_capabilities()["web_clause"]
        off = initial_messages([], "帮我找热点")[0]
        on = initial_messages([], "帮我找热点", web_enabled=True)[0]
        assert clause["judge_append"] not in off.user
        assert clause["judge_append"] in on.user


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

    def test_off_state_branch_asks_instead_of_refusing(self) -> None:
        """关闭态（`[web].enabled` 默认关）下「帮我找热点」必须有落点：问一句，别答成能力范围外。"""
        task = prompt_tool.load("chat_supervisor").sections["task"]
        assert "清单里没有这类工具" in task
        assert "你想蹭哪条热点" in task           # 走 §03.5 的问句，而不是拒绝
        assert "不要答成" in task                 # 明确禁止落到"不在这套工具的能力范围内"

    def test_fixed_refusal_phrase_carves_out_the_missing_optional_tool(self) -> None:
        """§03.1 固定话术的触发条件正是"清单里没有对应工具"——必须给关闭态留例外，否则反噬 §02。"""
        constraints = prompt_tool.load("chat_supervisor").sections["constraints"]
        assert "不在这套工具的能力范围内" in constraints      # 固定话术还在
        assert "唯一的例外" in constraints
        assert "缺一个可选工具" in constraints                # 少一个工具 ≠ 越界

    def test_no_phantom_candidate_reference(self) -> None:
        """关闭态根本没有候选，不能指示模型去转述一个不存在的对象。"""
        constraints = prompt_tool.load("chat_supervisor").sections["constraints"]
        assert "看到这几条候选" not in constraints
        assert "当候选转述" in constraints        # 换成依附于"真有搜索结果"的表述

    def test_selected_hotspot_has_an_explicit_subject(self) -> None:
        """决策流程第 (4) 步的"已经选定"要写明是谁选定的，不能让模型自选后直接开跑。"""
        task = prompt_tool.load("chat_supervisor").sections["task"]
        assert "用户认可" in task

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

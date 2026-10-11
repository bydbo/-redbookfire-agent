"""对话工具集的离线单测（S7.3 起，S7.7 扩到只读三件）：schema、参数校验、失败回话、缓存。

真链路（提交分析 → Celery → 轮询 → 汇总；拆解 → 检索 → 候选）在集成用例里跑；
这里只测不碰数据库的分支。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from xhs_agent.config import AppConfig, load_config
from xhs_agent.schemas import Coverage, Element
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import (
    HANDLERS,
    TOOL_PARSE_IMAGE,
    TOOL_PREVIEW_COVERAGE,
    TOOL_READ_RUN,
    TOOL_RUN_ANALYSIS,
    TOOL_SEARCH_MATERIALS,
    TOOLS,
    ToolContext,
    ToolOutcome,
    _clue_for,
    parse_clue_from_image,
    preview_element_coverage,
    read_run,
    run_hotspot_analysis,
    search_materials,
    tool_names,
)
from xhs_agent.services.retrieval import RetrievalOutcome
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult, StructuredCaller

CLUE_PAYLOAD: dict[str, Any] = {
    "hotspot_raw": "",
    "why_it_works": ["把日常器材拍出陌生感"],
    "mechanisms": [{"name": "反差", "explain": "器材特写与运动场景错位"}],
    "elements": [{"type": "visual", "value": "球拍特写", "weight": 0.9,
                  "confidence": 0.85, "evidence": "画面正中是球拍"}],
    "match_keywords": ["球拍", "球场"],
    "borrow_angles": ["用同款球场做热场"],
    "risk_notes": [],
}


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


def context(tmp_path: Path, **kwargs: Any) -> ToolContext:
    """只有 cfg 是真的：这些用例在用到 session 之前就该返回。"""
    return ToolContext(cfg=write_config(tmp_path), session=cast(Any, None), **kwargs)


class CountingProvider(llm.BaseProvider):
    """记账的假文本模型：固定回一段线索 JSON，只用来数"拆了几次"。"""

    name = "counting"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__(model="counting-1")
        self.payload = payload or CLUE_PAYLOAD
        self.calls = 0

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        self.calls += 1
        return LLMResult(text=json.dumps(self.payload, ensure_ascii=False),
                         provider=self.name, model=self.model,
                         prompt_tokens=10, completion_tokens=5, cost_cny=0.003)


class TestToolSchema:
    def test_every_tool_is_a_wellformed_function_schema(self) -> None:
        assert TOOLS, "工具清单不能为空"
        for item in TOOLS:
            assert item["type"] == "function"
            function = item["function"]
            assert function["name"] and function["description"].strip()
            assert len(function["description"]) > 20          # 描述要说清"什么时候用"
            parameters = function["parameters"]
            assert parameters["type"] == "object"
            assert isinstance(parameters["properties"], dict)
            assert isinstance(parameters.get("required", []), list)

    def test_registry_and_dispatcher_match(self) -> None:
        """清单与分发表一一对上：加工具时漏掉任一处都会被这条挡住。"""
        names = tool_names()
        assert names == {TOOL_RUN_ANALYSIS, TOOL_PARSE_IMAGE,
                         TOOL_SEARCH_MATERIALS, TOOL_READ_RUN,
                         TOOL_PREVIEW_COVERAGE}
        assert set(HANDLERS) == names

    def test_required_arguments_are_declared(self) -> None:
        by_name = {item["function"]["name"]: item["function"]["parameters"]
                   for item in TOOLS}
        assert by_name[TOOL_PARSE_IMAGE]["properties"] == {}          # 只吃本轮附件
        assert by_name[TOOL_SEARCH_MATERIALS]["required"] == ["hotspot"]
        assert by_name[TOOL_PREVIEW_COVERAGE]["required"] == ["hotspot"]
        assert by_name[TOOL_READ_RUN]["required"] == ["run_id"]
        assert by_name[TOOL_RUN_ANALYSIS]["required"] == []            # 只发图片时热点为空

    def test_preview_tool_says_it_produces_no_candidates(self) -> None:
        """S7.7b 的硬要求：说明里必须写明"不产出候选素材"，否则模型会把它当成分析的替代品。"""
        description = next(item["function"]["description"] for item in TOOLS
                           if item["function"]["name"] == TOOL_PREVIEW_COVERAGE)
        assert "不产出候选素材" in description
        assert TOOL_SEARCH_MATERIALS in description

    def test_tool_message_is_json(self) -> None:
        outcome = ToolOutcome(status="failed", payload={"run_id": "r-1"}, error="炸了")
        payload = json.loads(outcome.as_tool_message())
        assert payload == {"status": "failed", "run_id": "r-1", "error": "炸了"}

    def test_prompt_versions_do_not_leak_into_the_tool_message(self) -> None:
        outcome = ToolOutcome(status="succeeded", payload={"run_id": "r-1"},
                              prompt_versions={"hotspot_clue": 1})
        assert "prompt_versions" not in json.loads(outcome.as_tool_message())


class TestAnalyzeArguments:
    @pytest.mark.asyncio
    async def test_too_long_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path), {"hotspot": "热" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topk", [0, 21, "abc"])
    async def test_bad_topk_is_rejected(self, tmp_path: Path, topk: Any) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path),
                                             {"hotspot": "夜跑", "topk": topk})
        assert outcome.status == "failed" and "topk" in outcome.error

    @pytest.mark.asyncio
    async def test_without_text_and_image_asks_for_input(self, tmp_path: Path) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path), {})
        assert outcome.status == "failed"
        assert "问用户" in outcome.error or "想蹭哪条热点" in outcome.error

    @pytest.mark.asyncio
    async def test_unreadable_attachment_fails_politely(self, tmp_path: Path) -> None:
        ctx = context(tmp_path)
        outcome = await run_hotspot_analysis(
            ctx, {}, attachments=[{"path": "runs/_chat/没有这个文件.png", "kind": "image"}])
        assert outcome.status == "failed"
        assert "图片" in outcome.error
        assert outcome.steps and outcome.steps[0]["stage"] == "解析图片"


class TestReadOnlyArguments:
    @pytest.mark.asyncio
    async def test_search_without_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await search_materials(context(tmp_path), {})
        assert outcome.status == "failed" and "hotspot" in outcome.error

    @pytest.mark.asyncio
    async def test_search_with_too_long_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await search_materials(context(tmp_path), {"hotspot": "热" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topk", [0, 21, "abc"])
    async def test_search_with_bad_topk_is_rejected(self, tmp_path: Path,
                                                    topk: Any) -> None:
        outcome = await search_materials(context(tmp_path),
                                         {"hotspot": "夜跑", "topk": topk})
        assert outcome.status == "failed" and "topk" in outcome.error

    @pytest.mark.asyncio
    async def test_search_without_caller_fails_politely(self, tmp_path: Path) -> None:
        """运行器忘了注入 caller 时给一句能回给用户的话，而不是把异常往上抛。"""
        outcome = await search_materials(context(tmp_path), {"hotspot": "夜跑"})
        assert outcome.status == "failed"
        assert "拆解失败" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run_id", ["", "not-a-uuid", 123])
    async def test_read_run_with_bad_id_is_rejected(self, tmp_path: Path,
                                                    run_id: Any) -> None:
        outcome = await read_run(context(tmp_path), {"run_id": run_id})
        assert outcome.status == "failed" and "run_id" in outcome.error

    @pytest.mark.asyncio
    async def test_parse_image_without_attachment_is_rejected(self, tmp_path: Path) -> None:
        outcome = await parse_clue_from_image(context(tmp_path), {})
        assert outcome.status == "failed" and "图片" in outcome.error

    @pytest.mark.asyncio
    async def test_preview_without_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await preview_element_coverage(context(tmp_path), {})
        assert outcome.status == "failed" and "hotspot" in outcome.error

    @pytest.mark.asyncio
    async def test_preview_with_too_long_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await preview_element_coverage(context(tmp_path), {"hotspot": "热" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    async def test_preview_without_caller_fails_politely(self, tmp_path: Path) -> None:
        outcome = await preview_element_coverage(context(tmp_path), {"hotspot": "夜跑"})
        assert outcome.status == "failed" and "拆解失败" in outcome.error


class TestClueCache:
    @pytest.mark.asyncio
    async def test_same_hotspot_is_decomposed_once(self, tmp_path: Path) -> None:
        """S7.7 的"不重复付费"：同一轮里同一段原文只拆一次，第二次成本记 0。"""
        provider = CountingProvider()
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        first = await _clue_for(ctx, "夜跑的球拍特写")
        second = await _clue_for(ctx, "夜跑的球拍特写")
        third = await _clue_for(ctx, "另一条热点")

        assert provider.calls == 2                     # 第三段文本是新的一次拆解
        assert first.clue == second.clue
        assert first.cost_cny > 0 and second.cost_cny == 0.0
        assert first.version == 1 and second.version == 1
        assert third.cost_cny > 0


class TestPreviewShaping:
    @pytest.mark.asyncio
    async def test_gap_advice_is_derived_from_coverage_gaps(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺口建议必须由 `tools/gaps.py` 从覆盖度派生——这条不依赖匹配口径，是确定性的。

        真库上的覆盖率由 `tools/matching.py` 算（那里有"关键词命中给所有要素抬到 0.9"的既有行为），
        所以把检索结果换成替身来钉住"工具这一层怎么摆字段"。
        """
        provider = CountingProvider()
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))
        covered = Element(type="topic", value="羽毛球", weight=0.9, confidence=0.9,
                          evidence="原文")
        gap = Element(type="ip", value="某顶流明星", weight=0.9, confidence=0.9, evidence="原文")
        outcome = RetrievalOutcome(coverage=Coverage(covered=[covered], gaps=[gap], ratio=0.5))
        outcome.candidates_considered = 3

        async def fake_literal_only(*_args: Any, **_kwargs: Any) -> RetrievalOutcome:
            return outcome

        monkeypatch.setattr(chat_tools, "literal_only_candidates", fake_literal_only)
        result = await preview_element_coverage(ctx, {"hotspot": "羽毛球球场随手拍"})

        assert result.status == "succeeded", result.error
        assert result.payload["coverage_ratio"] == 0.5
        assert [item["type"] for item in result.payload["covered"]] == ["topic"]
        assert [item["type"] for item in result.payload["gaps"]] == ["ip"]
        advice = result.payload["gap_advice"]
        assert [item["type"] for item in advice] == ["ip"]
        assert advice[0]["value"] == "某顶流明星" and "同款" in advice[0]["advice"]
        assert result.payload["materials_considered"] == 3
        assert result.payload["cost_cny"] > 0          # 只花拆解那一次

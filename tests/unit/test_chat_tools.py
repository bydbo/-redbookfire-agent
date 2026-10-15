"""对话工具集的离线单测（S7.3 起，S7.7 扩到只读三件）：schema、参数校验、失败回话、缓存。

真链路（提交分析 → Celery → 轮询 → 汇总；拆解 → 检索 → 候选）在集成用例里跑；
这里只测不碰数据库的分支。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from xhs_agent.config import AppConfig, load_config
from xhs_agent.schemas import Coverage, Element, HotspotClue
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import (
    HANDLERS,
    TOOL_PARSE_IMAGE,
    TOOL_PREVIEW_COVERAGE,
    TOOL_READ_RUN,
    TOOL_REVISE_DRAFT,
    TOOL_RUN_ANALYSIS,
    TOOL_SEARCH_MATERIALS,
    TOOLS,
    ToolContext,
    ToolOutcome,
    _clue_for,
    parse_clue_from_image,
    preview_element_coverage,
    read_run,
    revise_draft,
    run_hotspot_analysis,
    search_materials,
    tool_names,
)
from xhs_agent.services.image_clue import ImageClueParse
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
                         TOOL_PREVIEW_COVERAGE, TOOL_REVISE_DRAFT}
        assert set(HANDLERS) == names

    def test_required_arguments_are_declared(self) -> None:
        by_name = {item["function"]["name"]: item["function"]["parameters"]
                   for item in TOOLS}
        assert by_name[TOOL_PARSE_IMAGE]["properties"] == {}          # 只吃本轮附件
        assert by_name[TOOL_SEARCH_MATERIALS]["required"] == ["hotspot"]
        assert by_name[TOOL_PREVIEW_COVERAGE]["required"] == ["hotspot"]
        # run_id 可省：不传就落到本会话最近一次运行产出的那个（S7.9 起）
        assert by_name[TOOL_READ_RUN]["required"] == []
        assert by_name[TOOL_REVISE_DRAFT]["required"] == ["request"]
        assert by_name[TOOL_RUN_ANALYSIS]["required"] == []            # 只发图片时热点为空

    def test_revise_tool_says_it_does_not_overwrite_the_run(self) -> None:
        """S7.8 的硬要求：说明里必须写明"不覆盖原结果"，否则模型会以为结果卡片会跟着变。"""
        description = next(item["function"]["description"] for item in TOOLS
                           if item["function"]["name"] == TOOL_REVISE_DRAFT)
        assert "不覆盖原结果" in description

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
        assert first.prompt_tokens > 0 and second.prompt_tokens == 0   # S7.10：用量只在第一次记
        assert first.version == 1 and second.version == 1
        assert third.cost_cny > 0


class TestImageUsageAndFallback:
    """图片解析的用量记账与跨轮回落（S7.10；S7.16 改成「整组」）。"""

    @staticmethod
    def _parsed() -> ImageClueParse:
        clue = HotspotClue.from_dict({**CLUE_PAYLOAD, "hotspot_raw": "夜跑球鞋开箱"})
        return ImageClueParse(raw_text="夜跑球鞋开箱", clue=clue,
                              prompt_versions={"image_hotspot_clue": 1},
                              prompt_tokens=1000, completion_tokens=500, cost_cny=0.0075)

    @pytest.mark.asyncio
    async def test_same_image_is_parsed_once_and_usage_is_zeroed(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一轮里同一张图只解析一次；第二次的用量记 0（钱只在上游真发生的那次记）。"""
        parsed = self._parsed()
        calls: list[str] = []

        async def fake_read(_ctx: ToolContext, _attachment: dict[str, Any]) -> Any:
            return b"jpeg-bytes", "image/jpeg"

        async def fake_parse(_cfg: Any, _data: bytes, _mime: str, *,
                             http: Any = None) -> ImageClueParse:
            calls.append(_mime)
            return parsed

        monkeypatch.setattr(chat_tools, "_read_image", fake_read)
        monkeypatch.setattr(chat_tools, "parse_image_clue", fake_parse)
        ctx = context(tmp_path)
        attachment = {"kind": "image", "path": "runs/_chat/s/图.png"}

        first = await chat_tools._image_clue_for(ctx, [attachment])
        second = await chat_tools._image_clue_for(ctx, [attachment])

        assert calls == ["image/jpeg"]
        assert first.prompt_versions == {"image_hotspot_clue": 1}
        assert first.prompt_tokens == 1000 and first.cost_cny == pytest.approx(0.0075)
        assert (second.prompt_tokens, second.completion_tokens, second.cost_cny) == (0, 0, 0.0)

    @pytest.mark.asyncio
    async def test_multi_image_group_goes_through_the_merge_path_once(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """多张图走合并 prompt（一次调用），同一组只解析一次；顺序变了算另一组。"""
        parsed = self._parsed()
        merged = ImageClueParse(raw_text="一组图的热点", clue=parsed.clue,
                                prompt_versions={"image_hotspot_clue_multi": 1},
                                prompt_tokens=3000, completion_tokens=600, cost_cny=0.02)
        single_calls: list[str] = []
        multi_calls: list[list[str]] = []

        async def fake_read(_ctx: ToolContext, attachment: dict[str, Any]) -> Any:
            return b"jpeg-bytes", "image/jpeg"

        async def fake_single(_cfg: Any, _data: bytes, _mime: str, *,
                              http: Any = None) -> ImageClueParse:
            single_calls.append(_mime)
            return parsed

        async def fake_multi(_cfg: Any, images: Any, *, http: Any = None) -> ImageClueParse:
            multi_calls.append([mime for _data, mime in images])
            return merged

        monkeypatch.setattr(chat_tools, "_read_image", fake_read)
        monkeypatch.setattr(chat_tools, "parse_image_clue", fake_single)
        monkeypatch.setattr(chat_tools, "parse_image_clue_multi", fake_multi)
        ctx = context(tmp_path)
        group = [{"kind": "image", "path": "runs/_chat/s/图1.png"},
                 {"kind": "image", "path": "runs/_chat/s/图2.png"}]

        first = await chat_tools._image_clue_for(ctx, group)
        second = await chat_tools._image_clue_for(ctx, group)          # 同一组：命中缓存
        reversed_group = list(reversed(group))
        await chat_tools._image_clue_for(ctx, reversed_group)          # 顺序变了：另一组

        assert single_calls == []                                      # 多图不走单图路径
        assert len(multi_calls) == 2 and multi_calls[0] == ["image/jpeg", "image/jpeg"]
        assert first.prompt_versions == {"image_hotspot_clue_multi": 1}
        assert first.cost_cny == pytest.approx(0.02)
        assert second.cost_cny == 0.0 and second.prompt_tokens == 0     # 缓存命中不计费

    @pytest.mark.asyncio
    async def test_analysis_passes_the_whole_group_to_the_parser(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """本轮带多张图时，交给解析的是**整组**（一次合并调用），不是第一张（S7.16）。"""
        parsed = self._parsed()
        merged = ImageClueParse(raw_text="一组图的热点", clue=parsed.clue,
                                prompt_versions={"image_hotspot_clue_multi": 1})
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(_ctx: ToolContext, attachments: list[dict[str, Any]]
                             ) -> ImageClueParse:
            seen.append(attachments)
            return merged

        captured: dict[str, Any] = {}

        class Stop(Exception):
            """哨兵：走到提交分析就够了。"""

        async def fake_submit(*args: Any, **kwargs: Any) -> Any:
            captured["args"] = args
            captured["kwargs"] = kwargs
            raise Stop

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        group = [{"kind": "image", "path": "runs/_chat/s/图1.png"},
                 {"kind": "image", "path": "runs/_chat/s/图2.png"},
                 {"kind": "image", "path": "runs/_chat/s/图3.png"}]

        with pytest.raises(Stop):
            await run_hotspot_analysis(context(tmp_path), {}, attachments=group)

        assert seen == [group]
        assert captured["kwargs"]["clues"] == [merged.clue.to_dict()]   # 预置线索来自整组解析

    @pytest.mark.asyncio
    async def test_parse_image_falls_back_to_the_recent_attachment(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        parsed = self._parsed()
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(_ctx: ToolContext, attachments: list[dict[str, Any]]
                             ) -> ImageClueParse:
            seen.append(attachments)
            return parsed

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        ctx = context(tmp_path, recent_attachments=[
            {"kind": "image", "path": "旧图1.png"}, {"kind": "image", "path": "旧图2.png"}])
        outcome = await parse_clue_from_image(ctx, {})

        assert outcome.status == "succeeded", outcome.error
        # 回落拿到的是**整组**，不是第一张
        assert seen == [[{"kind": "image", "path": "旧图1.png"},
                         {"kind": "image", "path": "旧图2.png"}]]
        assert outcome.payload["raw_text"] == "夜跑球鞋开箱"
        assert outcome.payload["image_count"] == 2
        assert outcome.payload["prompt_tokens"] == 1000
        assert outcome.payload["cost_cny"] == pytest.approx(0.0075)

    @pytest.mark.asyncio
    async def test_analysis_without_text_uses_the_recent_image(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        parsed = self._parsed()
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(_ctx: ToolContext, attachments: list[dict[str, Any]]
                             ) -> ImageClueParse:
            seen.append(attachments)
            return parsed

        class Stop(Exception):
            """哨兵：走到提交分析就够了，这里不碰库。"""

        async def fake_submit(*_args: Any, **_kwargs: Any) -> Any:
            raise Stop

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        ctx = context(tmp_path, recent_attachments=[{"kind": "image", "path": "旧图.png"}])

        with pytest.raises(Stop):
            await run_hotspot_analysis(ctx, {})
        assert seen == [[{"kind": "image", "path": "旧图.png"}]]

    @pytest.mark.asyncio
    async def test_analysis_with_text_ignores_the_recent_image(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """有热点文本时绝不混入旧图：否则会拿旧图的线索去跑一条新热点。"""
        parsed = self._parsed()
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(_ctx: ToolContext, attachments: list[dict[str, Any]]
                             ) -> ImageClueParse:
            seen.append(attachments)
            return parsed

        class Stop(Exception):
            """哨兵：走到提交分析就够了。"""

        async def fake_submit(*_args: Any, **_kwargs: Any) -> Any:
            raise Stop

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        ctx = context(tmp_path, recent_attachments=[{"kind": "image", "path": "旧图.png"}])

        with pytest.raises(Stop):
            await run_hotspot_analysis(ctx, {"hotspot": "一条全新的热点"})
        assert seen == []


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


# ---------- 改写工具（S7.8） ----------

RUN_UUID = "0f4d6f1e-6f2f-4b1a-9c3e-0d1a2b3c4d5e"
REVISED_PAYLOAD: dict[str, Any] = {
    "refused": False,
    "draft": {"titles": [{"text": "改后标题", "style": "避坑"}], "cover_text": "封面",
              "first_3s": "开头", "body": "改后的正文", "tags": ["#夜跑"],
              "shot_list": ["先拍球鞋"], "compliance_notes": ["别用他人肖像"]},
}
REFUSAL_PAYLOAD: dict[str, Any] = {
    "refused": True, "reason": "不能用他人的姓名或肖像做蹭热度文案",
}


def run_detail(*, status: str = "succeeded", with_draft: bool = True,
               hotspots: int = 1) -> dict[str, Any]:
    """`load_run` 形状的替身（只带工具真会读的字段）。"""
    items: list[dict[str, Any]] = []
    for position in range(1, hotspots + 1):
        raw = f"夜跑球鞋第 {position} 条"
        items.append({
            "hotspot_id": f"h-{position}",
            "hotspot_raw": raw,
            "clue": {"hotspot_raw": raw, "why_it_works": ["反差"],
                     "mechanisms": [{"name": "反差", "explain": "夜里跑步"}],
                     "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9,
                                   "confidence": 0.9, "evidence": "原文"}],
                     "match_keywords": ["夜跑"]},
            "coverage": {"ratio": 1.0, "covered": [], "gaps": []},
            "candidates": [{"material": {"id": "m-1", "path": "D:/m/夜跑.mp4",
                                         "type": "video", "title": "夜里的球鞋"}}],
            "draft": ({"material_id": "m-1", "hotspot_key": raw,
                       "titles": [{"text": "原标题", "style": "直给"}], "body": "原始正文",
                       "tags": ["#夜跑"], "cover_text": "", "first_3s": "", "shot_list": [],
                       "compliance_notes": []} if with_draft else None),
        })
    return {"run_id": RUN_UUID, "status": status, "created_at": "", "totals": {},
            "hotspots": items}


@pytest.fixture
def patched_run(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把 `load_run` 换成替身；返回一个可以改返回值的句柄。"""
    holder: dict[str, Any] = {"detail": run_detail()}

    async def fake_load_run(_session: Any, _run_id: Any) -> Any:
        return holder["detail"]

    monkeypatch.setattr(chat_tools, "load_run", fake_load_run)
    return holder


class TestReviseArguments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("run_id", ["", "not-a-uuid", 123])
    async def test_bad_run_id_is_rejected(self, tmp_path: Path, run_id: Any) -> None:
        outcome = await revise_draft(context(tmp_path), {"run_id": run_id, "request": "改短点"})
        assert outcome.status == "failed" and "run_id" in outcome.error

    @pytest.mark.asyncio
    async def test_missing_request_is_rejected(self, tmp_path: Path) -> None:
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID})
        assert outcome.status == "failed" and "request" in outcome.error

    @pytest.mark.asyncio
    async def test_too_long_request_is_rejected(self, tmp_path: Path) -> None:
        outcome = await revise_draft(context(tmp_path),
                                     {"run_id": RUN_UUID, "request": "改" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", [0, -1, "abc"])
    async def test_bad_hotspot_index_is_rejected(self, tmp_path: Path, index: Any) -> None:
        outcome = await revise_draft(context(tmp_path),
                                     {"run_id": RUN_UUID, "request": "改短点",
                                      "hotspot_index": index})
        assert outcome.status == "failed" and "hotspot_index" in outcome.error


class TestRevisePaths:
    @pytest.mark.asyncio
    async def test_returns_the_rewritten_draft(self, tmp_path: Path,
                                               patched_run: Any) -> None:
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"run_id": RUN_UUID, "request": "改成避坑向"})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["refused"] is False
        assert outcome.payload["revised"]["body"] == "改后的正文"
        assert outcome.payload["revised"]["material_id"] == "m-1"    # 关联字段按原稿写回
        assert outcome.payload["cost_cny"] > 0
        assert outcome.prompt_versions == {"copy_revise": 1}
        assert provider.calls == 1

    @pytest.mark.asyncio
    async def test_refusal_is_a_successful_outcome(self, tmp_path: Path,
                                                   patched_run: Any) -> None:
        provider = CountingProvider(REFUSAL_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"run_id": RUN_UUID,
                                           "request": "把明星名字写进标题"})

        assert outcome.status == "succeeded"          # 拒绝是一次成功的判断，不是工具故障
        assert outcome.payload["refused"] is True
        assert "肖像" in outcome.payload["reason"]
        assert "revised" not in outcome.payload
        assert outcome.prompt_versions == {"copy_revise": 1}

    @pytest.mark.asyncio
    async def test_missing_run_is_reported(self, tmp_path: Path,
                                           patched_run: Any) -> None:
        patched_run["detail"] = None
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID,
                                                         "request": "改短点"})
        assert outcome.status == "failed" and "找不到这次运行" in outcome.error

    @pytest.mark.asyncio
    async def test_unfinished_run_is_reported(self, tmp_path: Path,
                                              patched_run: Any) -> None:
        patched_run["detail"] = run_detail(status="running")
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID,
                                                         "request": "改短点"})
        assert outcome.status == "failed" and "还没跑完" in outcome.error

    @pytest.mark.asyncio
    async def test_index_out_of_range_is_reported(self, tmp_path: Path,
                                                  patched_run: Any) -> None:
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID,
                                                         "request": "改短点",
                                                         "hotspot_index": 3})
        assert outcome.status == "failed" and "只有 1 条热点" in outcome.error

    @pytest.mark.asyncio
    async def test_hotspot_without_draft_is_reported(self, tmp_path: Path,
                                                     patched_run: Any) -> None:
        patched_run["detail"] = run_detail(with_draft=False)
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID,
                                                         "request": "改短点"})
        assert outcome.status == "failed" and "没有文案初稿" in outcome.error

    @pytest.mark.asyncio
    async def test_without_caller_fails_politely(self, tmp_path: Path,
                                                 patched_run: Any) -> None:
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID,
                                                         "request": "改短点"})
        assert outcome.status == "failed" and "caller" in outcome.error


class TestRunIdFallback:
    """`run_id` 可省：不传就落到本会话最近一次运行产出的那个（S7.9 冒烟暴露的缺口）。"""

    @pytest.fixture
    def session_ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        """带伪会话上下文的 ToolContext + 可控的 `latest_run_id` / `load_run`。"""
        holder: dict[str, Any] = {"latest": RUN_UUID, "detail": run_detail(), "asked": None}

        async def fake_latest(_session: Any, session_id: Any) -> str:
            holder["asked"] = session_id
            return str(holder["latest"])

        async def fake_load_run(_session: Any, _run_id: Any) -> Any:
            return holder["detail"]

        monkeypatch.setattr(chat_tools, "latest_run_id", fake_latest)
        monkeypatch.setattr(chat_tools, "load_run", fake_load_run)
        holder["ctx_with_session"] = lambda **kw: context(       # noqa: E731 - 用例内的构造器
            tmp_path, session_id=uuid.uuid4(), **kw)
        return holder

    @pytest.mark.asyncio
    async def test_read_run_without_id_uses_the_latest_run(self, session_ctx: Any) -> None:
        outcome = await read_run(session_ctx["ctx_with_session"](), {})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["run_id"] == RUN_UUID
        assert session_ctx["asked"] is not None                  # 真的去查了"最近一次运行"

    @pytest.mark.asyncio
    async def test_read_run_without_id_and_no_run_is_reported(self, session_ctx: Any) -> None:
        session_ctx["latest"] = ""
        outcome = await read_run(session_ctx["ctx_with_session"](), {})

        assert outcome.status == "failed" and "还没跑过分析" in outcome.error

    @pytest.mark.asyncio
    async def test_revise_without_id_uses_the_latest_run(self, session_ctx: Any) -> None:
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = session_ctx["ctx_with_session"](caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"request": "改得更口语一点"})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["revised"]["body"] == "改后的正文"
        assert outcome.payload["run_id"] == RUN_UUID

    @pytest.mark.asyncio
    async def test_revise_without_id_and_no_run_is_reported(self, session_ctx: Any) -> None:
        session_ctx["latest"] = ""
        outcome = await revise_draft(session_ctx["ctx_with_session"](),
                                     {"request": "改得更口语一点"})

        assert outcome.status == "failed" and "还没跑过分析" in outcome.error


class FakePollSession:
    """只满足 `_wait_for_run` 的形状（`expire_all` 不发 SQL）。"""

    def __init__(self) -> None:
        self.expired = 0

    def expire_all(self) -> None:
        self.expired += 1


class TestWaitForRunStages:
    """S7.20：轮询阶段——排队等待 / 素材同步 / 拆解逐段上报，同一阶段不重复。"""

    @pytest.mark.asyncio
    async def test_reports_new_stages_once(self, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        jobs: list[dict[str, Any]] = [
            {"status": "queued", "progress": 0.0, "stage": None},
            {"status": "running", "progress": 0.05,
             "stage": {"stage": "material_sync", "status": "running"}},
            {"status": "running", "progress": 0.05,
             "stage": {"stage": "material_sync", "status": "running"}},
            {"status": "running", "progress": 0.4,
             "stage": {"stage": "clue", "status": "running"}},
            {"status": "succeeded", "progress": 1.0,
             "stage": {"stage": "report", "status": "succeeded"}},
        ]
        pending = list(jobs)

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return dict(pending.pop(0)) if pending else dict(jobs[-1])

        async def fake_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        session = FakePollSession()
        ctx = ToolContext(cfg=write_config(tmp_path), session=cast(Any, session),
                          sleep=fake_sleep, poll_interval_s=0.0)
        labels: list[str] = []

        async def progress(stage: str, done: int = 0, total: int = 0) -> None:
            labels.append(stage)

        status = await chat_tools._wait_for_run(ctx, "job-1", progress)

        assert status == "succeeded"
        assert labels == ["排队等待", "素材同步", "分析中", "拆解"]
        assert session.expired == 5              # 每次轮询都先过期，读到的是库里的新值

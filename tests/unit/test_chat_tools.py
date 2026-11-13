"""对话工具集的离线单测（S7.3 起，S7.7 扩到只读三件）：schema、参数校验、失败回话、缓存。

真链路（提交分析 → Celery → 轮询 → 汇总；拆解 → 检索 → 候选）在集成用例里跑；
这里只测不碰数据库的分支。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from xhs_agent.config import AppConfig, WebConfig, load_config
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
    TOOL_WRITE_DRAFT_ON_CANDIDATE,
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
    write_draft_on_candidate,
)
from xhs_agent.services.image_clue import ImageClueParse
from xhs_agent.services.retrieval import RetrievalOutcome
from xhs_agent.services.run_snapshots import evidence_meta
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMError, LLMResult, StructuredCaller

CLUE_PAYLOAD: dict[str, Any] = {
    "hotspot_raw": "",
    "why_it_works": ["把日常器材拍出陌生感"],
    "mechanisms": [{"name": "反差", "explain": "器材特写与运动场景错位"}],
    "elements": [
        {
            "type": "visual",
            "value": "球拍特写",
            "weight": 0.9,
            "confidence": 0.85,
            "evidence": "画面正中是球拍",
        }
    ],
    "match_keywords": ["球拍", "球场"],
    "borrow_angles": ["用同款球场做热场"],
    "risk_notes": [],
}


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
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
        return LLMResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            provider=self.name,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=5,
            cost_cny=0.003,
            usage_status="actual",  # R5：假 provider 按真实契约标来源
        )


class TestToolSchema:
    def test_every_tool_is_a_wellformed_function_schema(self) -> None:
        assert TOOLS, "工具清单不能为空"
        for item in TOOLS:
            assert item["type"] == "function"
            function = item["function"]
            assert function["name"] and function["description"].strip()
            assert len(function["description"]) > 20  # 描述要说清"什么时候用"
            parameters = function["parameters"]
            assert parameters["type"] == "object"
            assert isinstance(parameters["properties"], dict)
            assert isinstance(parameters.get("required", []), list)

    def test_registry_and_dispatcher_match(self) -> None:
        """清单与分发表一一对上：加工具时漏掉任一处都会被这条挡住。"""
        names = tool_names()
        assert names == {
            TOOL_RUN_ANALYSIS,
            TOOL_PARSE_IMAGE,
            TOOL_SEARCH_MATERIALS,
            TOOL_READ_RUN,
            TOOL_PREVIEW_COVERAGE,
            TOOL_REVISE_DRAFT,
            TOOL_WRITE_DRAFT_ON_CANDIDATE,
        }
        # PR2：网页两件的 schema 在 TOOLS 外面（按 [web] 开关追加），但 handler 常驻分发表，
        # 所以「一一对上」的等号右边取**开启态**清单；关闭态下发的仍是 TOOLS 本身。
        web_on = AppConfig()
        web_on.web = WebConfig(enabled=True)
        assert set(HANDLERS) == tool_names(web_on)

    def test_required_arguments_are_declared(self) -> None:
        by_name = {
            item["function"]["name"]: item["function"]["parameters"] for item in TOOLS
        }
        # A08：解析图片额外接受显式重解析与指定版本；都不是必填（默认用本轮图 + 当前选定版）
        assert set(by_name[TOOL_PARSE_IMAGE]["properties"]) == {
            "force_reparse", "clue_version_id"
        }
        assert by_name[TOOL_PARSE_IMAGE]["required"] == []
        assert by_name[TOOL_SEARCH_MATERIALS]["required"] == ["hotspot"]
        assert by_name[TOOL_PREVIEW_COVERAGE]["required"] == ["hotspot"]
        # run_id 可省：不传就落到本会话最近一次运行产出的那个（S7.9 起）
        assert by_name[TOOL_READ_RUN]["required"] == []
        assert by_name[TOOL_REVISE_DRAFT]["required"] == ["request"]
        assert by_name[TOOL_RUN_ANALYSIS]["required"] == []  # 只发图片时热点为空

    def test_revise_tool_says_it_does_not_overwrite_the_run(self) -> None:
        """S7.8 的硬要求：说明里必须写明"不覆盖原结果"，否则模型会以为结果卡片会跟着变。"""
        description = next(
            item["function"]["description"]
            for item in TOOLS
            if item["function"]["name"] == TOOL_REVISE_DRAFT
        )
        assert "不覆盖原结果" in description

    def test_preview_tool_says_it_produces_no_candidates(self) -> None:
        """S7.7b 的硬要求：说明里必须写明"不产出候选素材"，否则模型会把它当成分析的替代品。"""
        description = next(
            item["function"]["description"]
            for item in TOOLS
            if item["function"]["name"] == TOOL_PREVIEW_COVERAGE
        )
        assert "不产出候选素材" in description
        assert TOOL_SEARCH_MATERIALS in description

    def test_tool_message_is_json(self) -> None:
        outcome = ToolOutcome(status="failed", payload={"run_id": "r-1"}, error="炸了")
        payload = json.loads(outcome.as_tool_message())
        assert payload == {"status": "failed", "run_id": "r-1", "error": "炸了"}

    def test_prompt_versions_do_not_leak_into_the_tool_message(self) -> None:
        outcome = ToolOutcome(
            status="succeeded",
            payload={"run_id": "r-1"},
            prompt_versions={"hotspot_clue": 1},
        )
        assert "prompt_versions" not in json.loads(outcome.as_tool_message())


class TestAnalyzeArguments:
    @pytest.mark.asyncio
    async def test_too_long_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await run_hotspot_analysis(context(tmp_path), {"hotspot": "热" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topk", [0, 21, "abc"])
    async def test_bad_topk_is_rejected(self, tmp_path: Path, topk: Any) -> None:
        outcome = await run_hotspot_analysis(
            context(tmp_path), {"hotspot": "夜跑", "topk": topk}
        )
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
            ctx,
            {},
            attachments=[{"path": "runs/_chat/没有这个文件.png", "kind": "image"}],
        )
        assert outcome.status == "failed"
        assert "图片" in outcome.error
        assert outcome.steps and outcome.steps[0]["stage"] == "解析图片"


class TestReadOnlyArguments:
    @pytest.mark.asyncio
    async def test_search_without_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await search_materials(context(tmp_path), {})
        assert outcome.status == "failed" and "hotspot" in outcome.error

    @pytest.mark.asyncio
    async def test_search_with_too_long_hotspot_is_rejected(
        self, tmp_path: Path
    ) -> None:
        outcome = await search_materials(context(tmp_path), {"hotspot": "热" * 501})
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("topk", [0, 21, "abc"])
    async def test_search_with_bad_topk_is_rejected(
        self, tmp_path: Path, topk: Any
    ) -> None:
        outcome = await search_materials(
            context(tmp_path), {"hotspot": "夜跑", "topk": topk}
        )
        assert outcome.status == "failed" and "topk" in outcome.error

    @pytest.mark.asyncio
    async def test_search_without_caller_fails_politely(self, tmp_path: Path) -> None:
        """运行器忘了注入 caller 时给一句能回给用户的话，而不是把异常往上抛。"""
        outcome = await search_materials(context(tmp_path), {"hotspot": "夜跑"})
        assert outcome.status == "failed"
        assert "拆解失败" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run_id", ["", "not-a-uuid", 123])
    async def test_read_run_with_bad_id_is_rejected(
        self, tmp_path: Path, run_id: Any
    ) -> None:
        outcome = await read_run(context(tmp_path), {"run_id": run_id})
        assert outcome.status == "failed" and "run_id" in outcome.error

    @pytest.mark.asyncio
    async def test_parse_image_without_attachment_is_rejected(
        self, tmp_path: Path
    ) -> None:
        outcome = await parse_clue_from_image(context(tmp_path), {})
        assert outcome.status == "failed" and "图片" in outcome.error

    @pytest.mark.asyncio
    async def test_preview_without_hotspot_is_rejected(self, tmp_path: Path) -> None:
        outcome = await preview_element_coverage(context(tmp_path), {})
        assert outcome.status == "failed" and "hotspot" in outcome.error

    @pytest.mark.asyncio
    async def test_preview_with_too_long_hotspot_is_rejected(
        self, tmp_path: Path
    ) -> None:
        outcome = await preview_element_coverage(
            context(tmp_path), {"hotspot": "热" * 501}
        )
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

        assert provider.calls == 2  # 第三段文本是新的一次拆解
        assert first.clue == second.clue
        assert first.cost_cny > 0 and second.cost_cny == 0.0
        assert (
            first.prompt_tokens > 0 and second.prompt_tokens == 0
        )  # S7.10：用量只在第一次记
        assert first.version == 2 and second.version == 2
        assert third.cost_cny > 0
        assert first.usage_status == "actual"  # R5：真实调用带口径
        assert second.usage_status == "cached"  # 缓存命中：本轮无新增消费


class TestImageUsageAndFallback:
    """图片解析的用量记账与跨轮回落（S7.10；S7.16 改成「整组」）。"""

    @staticmethod
    def _parsed() -> ImageClueParse:
        clue = HotspotClue.from_dict({**CLUE_PAYLOAD, "hotspot_raw": "夜跑球鞋开箱"})
        return ImageClueParse(
            raw_text="夜跑球鞋开箱",
            clue=clue,
            prompt_versions={"image_hotspot_clue": 1},
            prompt_tokens=1000,
            completion_tokens=500,
            cost_cny=0.0075,
            usage_status="actual",  # R5：视觉结果自带来源口径
        )

    @pytest.mark.asyncio
    async def test_same_image_is_parsed_once_and_usage_is_zeroed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一轮里同一张图只解析一次；第二次的用量记 0（钱只在上游真发生的那次记）。"""
        parsed = self._parsed()
        calls: list[str] = []

        async def fake_read(_ctx: ToolContext, _attachment: dict[str, Any]) -> Any:
            return b"jpeg-bytes", "image/jpeg"

        async def fake_parse(
            _cfg: Any, _data: bytes, _mime: str, *, http: Any = None
        ) -> ImageClueParse:
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
        assert (second.prompt_tokens, second.completion_tokens, second.cost_cny) == (
            0,
            0,
            0.0,
        )
        assert first.usage_status == "actual"
        assert second.usage_status == "cached"  # R5：命中缓存与免费真实请求区分

    @pytest.mark.asyncio
    async def test_multi_image_group_goes_through_the_merge_path_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """多张图走合并 prompt（一次调用），同一组只解析一次；顺序变了算另一组。"""
        parsed = self._parsed()
        merged = ImageClueParse(
            raw_text="一组图的热点",
            clue=parsed.clue,
            prompt_versions={"image_hotspot_clue_multi": 1},
            prompt_tokens=3000,
            completion_tokens=600,
            cost_cny=0.02,
            usage_status="estimated",
        )
        single_calls: list[str] = []
        multi_calls: list[list[str]] = []

        async def fake_read(_ctx: ToolContext, attachment: dict[str, Any]) -> Any:
            return b"jpeg-bytes", "image/jpeg"

        async def fake_single(
            _cfg: Any, _data: bytes, _mime: str, *, http: Any = None
        ) -> ImageClueParse:
            single_calls.append(_mime)
            return parsed

        async def fake_multi(
            _cfg: Any, images: Any, *, http: Any = None
        ) -> ImageClueParse:
            multi_calls.append([mime for _data, mime in images])
            return merged

        monkeypatch.setattr(chat_tools, "_read_image", fake_read)
        monkeypatch.setattr(chat_tools, "parse_image_clue", fake_single)
        monkeypatch.setattr(chat_tools, "parse_image_clue_multi", fake_multi)
        ctx = context(tmp_path)
        group = [
            {"kind": "image", "path": "runs/_chat/s/图1.png"},
            {"kind": "image", "path": "runs/_chat/s/图2.png"},
        ]

        first = await chat_tools._image_clue_for(ctx, group)
        second = await chat_tools._image_clue_for(ctx, group)  # 同一组：命中缓存
        reversed_group = list(reversed(group))
        await chat_tools._image_clue_for(ctx, reversed_group)  # 顺序变了：另一组

        assert single_calls == []  # 多图不走单图路径
        assert len(multi_calls) == 2 and multi_calls[0] == ["image/jpeg", "image/jpeg"]
        assert first.prompt_versions == {"image_hotspot_clue_multi": 1}
        assert first.cost_cny == pytest.approx(0.02)
        assert second.cost_cny == 0.0 and second.prompt_tokens == 0  # 缓存命中不计费
        assert first.usage_status == "estimated"
        assert second.usage_status == "cached"  # R5：缓存命中口径随结果上卷

    @pytest.mark.asyncio
    async def test_analysis_passes_the_whole_group_to_the_parser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """本轮带多张图时，交给解析的是**整组**（一次合并调用），不是第一张（S7.16）。"""
        parsed = self._parsed()
        merged = ImageClueParse(
            raw_text="一组图的热点",
            clue=parsed.clue,
            prompt_versions={"image_hotspot_clue_multi": 1},
        )
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(
            _ctx: ToolContext, attachments: list[dict[str, Any]], **kwargs: Any
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
        group = [
            {"kind": "image", "path": "runs/_chat/s/图1.png"},
            {"kind": "image", "path": "runs/_chat/s/图2.png"},
            {"kind": "image", "path": "runs/_chat/s/图3.png"},
        ]

        with pytest.raises(Stop):
            await run_hotspot_analysis(context(tmp_path), {}, attachments=group)

        assert seen == [group]
        assert captured["kwargs"]["clues"] == [
            merged.clue.to_dict()
        ]  # 预置线索来自整组解析

    @pytest.mark.asyncio
    async def test_parse_image_falls_back_to_the_recent_attachment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = self._parsed()
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(
            _ctx: ToolContext, attachments: list[dict[str, Any]], **kwargs: Any
        ) -> ImageClueParse:
            seen.append(attachments)
            return parsed

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        ctx = context(
            tmp_path,
            recent_attachments=[
                {"kind": "image", "path": "旧图1.png"},
                {"kind": "image", "path": "旧图2.png"},
            ],
        )
        outcome = await parse_clue_from_image(ctx, {})

        assert outcome.status == "succeeded", outcome.error
        # 回落拿到的是**整组**，不是第一张
        assert seen == [
            [
                {"kind": "image", "path": "旧图1.png"},
                {"kind": "image", "path": "旧图2.png"},
            ]
        ]
        assert outcome.payload["raw_text"] == "夜跑球鞋开箱"
        assert outcome.payload["image_count"] == 2
        assert outcome.payload["prompt_tokens"] == 1000
        assert outcome.payload["cost_cny"] == pytest.approx(0.0075)

    @pytest.mark.asyncio
    async def test_analysis_without_text_uses_the_recent_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed = self._parsed()
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(
            _ctx: ToolContext, attachments: list[dict[str, Any]], **kwargs: Any
        ) -> ImageClueParse:
            seen.append(attachments)
            return parsed

        class Stop(Exception):
            """哨兵：走到提交分析就够了，这里不碰库。"""

        async def fake_submit(*_args: Any, **_kwargs: Any) -> Any:
            raise Stop

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        ctx = context(
            tmp_path, recent_attachments=[{"kind": "image", "path": "旧图.png"}]
        )

        with pytest.raises(Stop):
            await run_hotspot_analysis(ctx, {})
        assert seen == [[{"kind": "image", "path": "旧图.png"}]]

    @pytest.mark.asyncio
    async def test_analysis_with_text_ignores_the_recent_image(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有热点文本时绝不混入旧图：否则会拿旧图的线索去跑一条新热点。"""
        parsed = self._parsed()
        seen: list[list[dict[str, Any]]] = []

        async def fake_image(
            _ctx: ToolContext, attachments: list[dict[str, Any]], **kwargs: Any
        ) -> ImageClueParse:
            seen.append(attachments)
            return parsed

        class Stop(Exception):
            """哨兵：走到提交分析就够了。"""

        async def fake_submit(*_args: Any, **_kwargs: Any) -> Any:
            raise Stop

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        ctx = context(
            tmp_path, recent_attachments=[{"kind": "image", "path": "旧图.png"}]
        )

        with pytest.raises(Stop):
            await run_hotspot_analysis(ctx, {"hotspot": "一条全新的热点"})
        assert seen == []


class TestPreviewShaping:
    @pytest.mark.asyncio
    async def test_gap_advice_is_derived_from_coverage_gaps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺口建议必须由 `tools/gaps.py` 从覆盖度派生——这条不依赖匹配口径，是确定性的。

        真库上的覆盖率由 `tools/matching.py` 算（那里有"关键词命中给所有要素抬到 0.9"的既有行为），
        所以把检索结果换成替身来钉住"工具这一层怎么摆字段"。
        """
        provider = CountingProvider()
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))
        covered = Element(
            type="topic", value="羽毛球", weight=0.9, confidence=0.9, evidence="原文"
        )
        gap = Element(
            type="ip", value="某顶流明星", weight=0.9, confidence=0.9, evidence="原文"
        )
        outcome = RetrievalOutcome(
            coverage=Coverage(covered=[covered], gaps=[gap], ratio=0.5)
        )
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
        assert result.payload["cost_cny"] > 0  # 只花拆解那一次


# ---------- 改写工具（S7.8） ----------

RUN_UUID = "0f4d6f1e-6f2f-4b1a-9c3e-0d1a2b3c4d5e"
REVISED_PAYLOAD: dict[str, Any] = {
    "refused": False,
    "draft": {
        "titles": [{"text": "改后标题", "style": "避坑"}],
        "cover_text": "封面",
        "first_3s": "开头",
        "body": "改后的正文",
        "tags": ["#夜跑"],
        "shot_list": ["先拍球鞋"],
        "compliance_notes": ["别用他人肖像"],
    },
}
REFUSAL_PAYLOAD: dict[str, Any] = {
    "refused": True,
    "reason": "不能用他人的姓名或肖像做蹭热度文案",
}


FIRST_MATERIAL_ID = "11111111-1111-1111-1111-111111111111"
# 候选标题按 rank 排开：用户说「第 4 条」时的对齐口径就是 rank，标题必须互不相同才验得出来
CANDIDATE_TITLES = ("夜里的球鞋", "路灯下的鞋带", "跑鞋特写", "清晨的跑道")


def candidate_row(rank: int, title: str) -> dict[str, Any]:
    """`_matches_by_run_hotspot` 形状的候选替身（rank / score / usage 都要能被模型看到）。"""
    material_id = (
        FIRST_MATERIAL_ID if rank == 1 else f"{rank:08x}-2222-2222-2222-222222222222"
    )
    return {
        "rank": rank,
        "material_id": material_id,
        "score": round(0.95 - 0.1 * (rank - 1), 3),
        "usage": f"第 {rank} 条的用途摘述",
        "material": {
            "id": material_id,
            "path": f"D:/m/{title}.mp4",
            "type": "video",
            "title": title,
            "description": "夜里跑步",
            "tags": ["夜跑"],
            "elements": [],
            "duration_s": 12.0,
            "width": 1080,
            "height": 1920,
            "has_audio": True,
            "size_bytes": 10,
            "mtime": None,
            "source": "sidecar",
            "keyframes": [],
            "indexed_at": None,
            "fingerprint": "fp",
            "embedding_model": None,
        },
        "current_material": {"exists": True},
        "evidence": {"candidate": {"origin": "runtime"}},
    }


def run_detail(
    *,
    status: str = "succeeded",
    with_draft: bool = True,
    hotspots: int = 1,
    candidates: int = len(CANDIDATE_TITLES),
) -> dict[str, Any]:
    """`load_run` 形状的替身（只带工具真会读的字段）。"""
    items: list[dict[str, Any]] = []
    for position in range(1, hotspots + 1):
        raw = f"夜跑球鞋第 {position} 条"
        items.append(
            {
                "hotspot_id": f"h-{position}",
                "hotspot_raw": raw,
                "clue": {
                    "hotspot_raw": raw,
                    "why_it_works": ["反差"],
                    "mechanisms": [{"name": "反差", "explain": "夜里跑步"}],
                    "elements": [
                        {
                            "type": "topic",
                            "value": "夜跑",
                            "weight": 0.9,
                            "confidence": 0.9,
                            "evidence": "原文",
                        }
                    ],
                    "match_keywords": ["夜跑"],
                },
                "position": position,
                "status": "succeeded",
                "error": None,
                "coverage": {"ratio": 1.0, "covered": [], "gaps": []},
                "retrieval_meta": {},
                "gap_advice": {"items": []},
                "evidence": {"clue": {"origin": "runtime"}},
                "candidates": [
                    candidate_row(rank, title)
                    for rank, title in enumerate(CANDIDATE_TITLES[:candidates], start=1)
                ],
                "draft": (
                    {
                        "material_id": FIRST_MATERIAL_ID,
                        "hotspot_key": raw,
                        "titles": [{"text": "原标题", "style": "直给"}],
                        "body": "原始正文",
                        "tags": ["#夜跑"],
                        "cover_text": "",
                        "first_3s": "",
                        "shot_list": [],
                        "compliance_notes": [],
                    }
                    if with_draft
                    else None
                ),
            }
        )
    for item in items:
        item["evidence"]["clue"] = evidence_meta(
            origin="runtime", source_ref="unit:clue", completeness="complete", payload=item["clue"])
        for candidate in item["candidates"]:
            candidate["evidence"]["candidate"] = evidence_meta(
                origin="runtime", source_ref="unit:material", completeness="complete",
                payload=candidate["material"])
    return {
        "run_id": RUN_UUID,
        "status": status,
        "created_at": "",
        "totals": {},
        "execution_snapshot": {},
        "evidence": {},
        "hotspots": items,
    }


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
        outcome = await revise_draft(
            context(tmp_path), {"run_id": run_id, "request": "改短点"}
        )
        assert outcome.status == "failed" and "run_id" in outcome.error

    @pytest.mark.asyncio
    async def test_missing_request_is_rejected(self, tmp_path: Path) -> None:
        outcome = await revise_draft(context(tmp_path), {"run_id": RUN_UUID})
        assert outcome.status == "failed" and "request" in outcome.error

    @pytest.mark.asyncio
    async def test_too_long_request_is_rejected(self, tmp_path: Path) -> None:
        outcome = await revise_draft(
            context(tmp_path), {"run_id": RUN_UUID, "request": "改" * 501}
        )
        assert outcome.status == "failed" and "500" in outcome.error

    @pytest.mark.asyncio
    @pytest.mark.parametrize("index", [0, -1, "abc"])
    async def test_bad_hotspot_index_is_rejected(
        self, tmp_path: Path, index: Any
    ) -> None:
        outcome = await revise_draft(
            context(tmp_path),
            {"run_id": RUN_UUID, "request": "改短点", "hotspot_index": index},
        )
        assert outcome.status == "failed" and "hotspot_index" in outcome.error


class TestRevisePaths:
    @pytest.mark.asyncio
    async def test_returns_the_rewritten_draft(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"run_id": RUN_UUID, "request": "改成避坑向"})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["refused"] is False
        assert outcome.payload["revised"]["body"] == "改后的正文"
        assert outcome.payload["revised"]["material_id"] == "11111111-1111-1111-1111-111111111111"
        assert outcome.payload["cost_cny"] > 0
        assert outcome.prompt_versions == {"copy_revise": 3}
        assert provider.calls == 1

    @pytest.mark.asyncio
    async def test_refusal_is_a_successful_outcome(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        provider = CountingProvider(REFUSAL_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(
            ctx, {"run_id": RUN_UUID, "request": "把明星名字写进标题"}
        )

        assert outcome.status == "succeeded"  # 拒绝是一次成功的判断，不是工具故障
        assert outcome.payload["refused"] is True
        assert "肖像" in outcome.payload["reason"]
        assert "revised" not in outcome.payload
        assert outcome.prompt_versions == {"copy_revise": 3}

    @pytest.mark.asyncio
    async def test_missing_run_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        patched_run["detail"] = None
        outcome = await revise_draft(
            context(tmp_path), {"run_id": RUN_UUID, "request": "改短点"}
        )
        assert outcome.status == "failed" and "找不到这次运行" in outcome.error

    @pytest.mark.asyncio
    async def test_unfinished_run_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        patched_run["detail"] = run_detail(status="running")
        outcome = await revise_draft(
            context(tmp_path), {"run_id": RUN_UUID, "request": "改短点"}
        )
        assert outcome.status == "failed" and "还没跑完" in outcome.error

    @pytest.mark.asyncio
    async def test_index_out_of_range_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        outcome = await revise_draft(
            context(tmp_path),
            {"run_id": RUN_UUID, "request": "改短点", "hotspot_index": 3},
        )
        assert outcome.status == "failed" and "只有 1 条热点" in outcome.error

    @pytest.mark.asyncio
    async def test_hotspot_without_draft_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        patched_run["detail"] = run_detail(with_draft=False)
        outcome = await revise_draft(
            context(tmp_path), {"run_id": RUN_UUID, "request": "改短点"}
        )
        assert outcome.status == "failed" and "没有文案初稿" in outcome.error

    @pytest.mark.asyncio
    async def test_without_caller_fails_politely(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        outcome = await revise_draft(
            context(tmp_path), {"run_id": RUN_UUID, "request": "改短点"}
        )
        assert outcome.status == "failed" and "caller" in outcome.error


# ---------- 按候选写稿（S7.32） ----------

DRAFT_ON_CANDIDATE_PAYLOAD: dict[str, Any] = {
    "titles": [{"text": "树上挂着的外套", "style": "直给"}],
    "cover_text": "树上外套",
    "first_3s": "树上挂着的浅蓝羽绒服",
    "body": "这版按第 4 条写：树上挂着浅蓝羽绒服。",
    "tags": ["#夜跑"],
    "shot_list": ["先拍树上的外套"],
    "compliance_notes": ["别带品牌 logo"],
}


class _CountResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _CountSession:
    """只满足 `_run_belongs_to_session` 的会话替身（一条 count 查询）。"""

    def __init__(self, value: int) -> None:
        self._value = value

    async def execute(self, *_args: Any, **_kwargs: Any) -> _CountResult:
        return _CountResult(self._value)


class TestWriteDraftOnCandidate:
    """以某条候选素材为基准写新稿：序号越界、证据缺失、归属、不改库。"""

    def test_schema_requires_material_rank(self) -> None:
        schema = next(
            item["function"] for item in TOOLS
            if item["function"]["name"] == TOOL_WRITE_DRAFT_ON_CANDIDATE
        )
        assert schema["parameters"]["required"] == ["material_rank"]
        assert TOOL_WRITE_DRAFT_ON_CANDIDATE in HANDLERS

    @pytest.mark.asyncio
    async def test_writes_a_draft_bound_to_the_named_candidate(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        provider = CountingProvider(DRAFT_ON_CANDIDATE_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await write_draft_on_candidate(
            ctx, {"run_id": RUN_UUID, "material_rank": 4}
        )

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["material_rank"] == 4
        assert outcome.payload["material"]["material_id"] == (
            "00000004-2222-2222-2222-222222222222"
        )
        assert outcome.payload["material"]["title"] == "清晨的跑道"
        # 新稿必须绑在第 4 条上：绑错等于换素材白做
        assert outcome.payload["draft"]["material_id"] == (
            "00000004-2222-2222-2222-222222222222"
        )
        assert outcome.payload["draft"]["body"] == "这版按第 4 条写：树上挂着浅蓝羽绒服。"
        assert outcome.prompt_versions == {"copy_draft": 3}
        # 无会话上下文 → 不登记版本（与 revise_draft 的 off 分支同口径）
        assert outcome.payload["version_id"] is None
        assert provider.calls == 1

    @pytest.mark.asyncio
    async def test_rank_out_of_range_lists_what_exists(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        provider = CountingProvider(DRAFT_ON_CANDIDATE_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await write_draft_on_candidate(
            ctx, {"run_id": RUN_UUID, "material_rank": 9}
        )

        assert outcome.status == "failed"
        assert outcome.payload["reason"] == "material_rank_out_of_range"
        assert outcome.payload["count"] == len(CANDIDATE_TITLES)
        assert [row["rank"] for row in outcome.payload["candidates"]] == [1, 2, 3, 4]
        assert "没有第 9 条" in outcome.error
        assert provider.calls == 0  # 校验不过不许花钱

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rank", [0, -1, "abc"])
    async def test_bad_material_rank_is_rejected(
        self, tmp_path: Path, patched_run: Any, rank: Any
    ) -> None:
        outcome = await write_draft_on_candidate(
            context(tmp_path), {"run_id": RUN_UUID, "material_rank": rank}
        )
        assert outcome.status == "failed" and "material_rank" in outcome.error

    @pytest.mark.asyncio
    async def test_missing_material_rank_is_rejected(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        outcome = await write_draft_on_candidate(context(tmp_path), {"run_id": RUN_UUID})
        assert outcome.status == "failed" and "缺少 material_rank" in outcome.error

    @pytest.mark.asyncio
    async def test_unfinished_run_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        patched_run["detail"] = run_detail(status="running")
        outcome = await write_draft_on_candidate(
            context(tmp_path), {"run_id": RUN_UUID, "material_rank": 1}
        )
        assert outcome.status == "failed" and "还没跑完" in outcome.error

    @pytest.mark.asyncio
    async def test_candidate_without_snapshot_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        detail = run_detail()
        detail["hotspots"][0]["candidates"][3]["material"] = None
        patched_run["detail"] = detail
        provider = CountingProvider(DRAFT_ON_CANDIDATE_PAYLOAD)
        ctx = context(tmp_path, caller=StructuredCaller(provider=provider))

        outcome = await write_draft_on_candidate(
            ctx, {"run_id": RUN_UUID, "material_rank": 4}
        )

        assert outcome.status == "failed"
        assert outcome.payload["reason"] == "material_snapshot_missing"
        assert provider.calls == 0

    @pytest.mark.asyncio
    async def test_missing_clue_is_reported(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        detail = run_detail()
        detail["hotspots"][0]["clue"] = None
        patched_run["detail"] = detail

        outcome = await write_draft_on_candidate(
            context(tmp_path), {"run_id": RUN_UUID, "material_rank": 1}
        )
        assert outcome.status == "failed" and "没有爆点线索" in outcome.error

    @pytest.mark.asyncio
    async def test_without_caller_fails_politely(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        outcome = await write_draft_on_candidate(
            context(tmp_path), {"run_id": RUN_UUID, "material_rank": 1}
        )
        assert outcome.status == "failed" and "caller" in outcome.error

    @pytest.mark.asyncio
    async def test_run_outside_the_session_is_refused(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        ctx = ToolContext(
            cfg=write_config(tmp_path),
            session=cast(Any, _CountSession(0)),
            session_id=uuid.uuid4(),
        )

        outcome = await write_draft_on_candidate(
            ctx, {"run_id": RUN_UUID, "material_rank": 1}
        )

        assert outcome.status == "failed"
        assert outcome.payload["reason"] == "run_not_in_session"

    @pytest.mark.asyncio
    async def test_run_snapshot_is_not_mutated(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        before = json.loads(json.dumps(patched_run["detail"], ensure_ascii=False, default=str))
        provider = CountingProvider(DRAFT_ON_CANDIDATE_PAYLOAD)

        await write_draft_on_candidate(
            context(tmp_path, caller=StructuredCaller(provider=provider)),
            {"run_id": RUN_UUID, "material_rank": 3},
        )

        after = json.loads(json.dumps(patched_run["detail"], ensure_ascii=False, default=str))
        assert after == before  # run 是冻结快照，写稿不许回写


class TestRunIdFallback:
    """`run_id` 可省：不传就落到本会话最近一次运行产出的那个（S7.9 冒烟暴露的缺口）。"""

    @pytest.fixture
    def session_ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        """带伪会话上下文的 ToolContext + 可控的 `latest_run_id` / `load_run`。"""
        holder: dict[str, Any] = {
            "latest": RUN_UUID,
            "detail": run_detail(),
            "asked": None,
        }

        async def fake_latest(_session: Any, session_id: Any) -> str:
            holder["asked"] = session_id
            return str(holder["latest"])

        async def fake_load_run(_session: Any, _run_id: Any) -> Any:
            return holder["detail"]

        monkeypatch.setattr(chat_tools, "latest_run_id", fake_latest)
        monkeypatch.setattr(chat_tools, "load_run", fake_load_run)
        holder["ctx_with_session"] = lambda **kw: context(  # noqa: E731 - 用例内的构造器
            tmp_path, session_id=uuid.uuid4(), **kw
        )
        return holder

    @pytest.mark.asyncio
    async def test_read_run_without_id_uses_the_latest_run(
        self, session_ctx: Any
    ) -> None:
        outcome = await read_run(session_ctx["ctx_with_session"](), {})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["run_id"] == RUN_UUID
        assert session_ctx["asked"] is not None  # 真的去查了"最近一次运行"

    @pytest.mark.asyncio
    async def test_read_run_without_id_and_no_run_is_reported(
        self, session_ctx: Any
    ) -> None:
        session_ctx["latest"] = ""
        outcome = await read_run(session_ctx["ctx_with_session"](), {})

        assert outcome.status == "failed" and "还没跑过分析" in outcome.error

    @pytest.mark.asyncio
    async def test_revise_without_id_uses_the_latest_run(
        self, session_ctx: Any
    ) -> None:
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = session_ctx["ctx_with_session"](
            caller=StructuredCaller(provider=provider)
        )

        outcome = await revise_draft(ctx, {"request": "改得更口语一点"})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["revised"]["body"] == "改后的正文"
        assert outcome.payload["run_id"] == RUN_UUID

    @pytest.mark.asyncio
    async def test_revise_without_id_and_no_run_is_reported(
        self, session_ctx: Any
    ) -> None:
        session_ctx["latest"] = ""
        outcome = await revise_draft(
            session_ctx["ctx_with_session"](), {"request": "改得更口语一点"}
        )

        assert outcome.status == "failed" and "还没跑过分析" in outcome.error


class TestReadRunCandidateList:
    """P0-1：`read_run` 要整份带回候选清单。

    用户指「第 4 条」时，那次运行的 `rank` 是唯一对齐口径；只回「条数 + 首选」，模型就只能重新
    检索，拿到的是另一份排序，再拿新排序去否定用户——「把对的说成错的」就是这么来的。
    """

    @pytest.mark.asyncio
    async def test_list_keeps_the_run_order(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        outcome = await read_run(context(tmp_path), {"run_id": RUN_UUID})

        assert outcome.status == "succeeded", outcome.error
        hotspot = cast(dict[str, Any], outcome.payload)["hotspots"][0]
        assert hotspot["count"] == len(CANDIDATE_TITLES)
        assert [item["rank"] for item in hotspot["candidates"]] == [1, 2, 3, 4]
        assert [item["title"] for item in hotspot["candidates"]] == list(
            CANDIDATE_TITLES
        )
        # 首选与清单第一行必须同一份数据：两处说法不一致等于自造矛盾
        assert hotspot["top1_title"] == hotspot["candidates"][0]["title"]

    @pytest.mark.asyncio
    async def test_the_fourth_row_is_answerable(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        outcome = await read_run(context(tmp_path), {"run_id": RUN_UUID})

        fourth = cast(dict[str, Any], outcome.payload)["hotspots"][0]["candidates"][3]
        assert fourth["rank"] == 4
        assert fourth["title"] == "清晨的跑道"
        # usage 是「标题没说、但用户会用来指认」的那句话，必须透出
        assert fourth["usage"] == "第 4 条的用途摘述"
        assert fourth["material_id"] == "00000004-2222-2222-2222-222222222222"
        assert fourth["score"] == 0.65

    @pytest.mark.asyncio
    async def test_long_list_is_capped_while_count_keeps_the_total(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        detail = run_detail(candidates=0)
        detail["hotspots"][0]["candidates"] = [
            candidate_row(rank, f"候选 {rank}") for rank in range(1, 12)
        ]
        patched_run["detail"] = detail

        outcome = await read_run(context(tmp_path), {"run_id": RUN_UUID})

        hotspot = cast(dict[str, Any], outcome.payload)["hotspots"][0]
        assert hotspot["count"] == 11
        assert len(hotspot["candidates"]) == chat_tools.CANDIDATE_PREVIEW_LIMIT

    @pytest.mark.asyncio
    async def test_empty_title_falls_back_to_the_file_name(
        self, tmp_path: Path, patched_run: Any
    ) -> None:
        detail = run_detail(candidates=0)
        row = candidate_row(1, "夜里的球鞋")
        row["material"]["title"] = ""
        row["material"]["path"] = "D:/m/无名素材.mp4"
        detail["hotspots"][0]["candidates"] = [row]
        patched_run["detail"] = detail

        outcome = await read_run(context(tmp_path), {"run_id": RUN_UUID})

        hotspot = cast(dict[str, Any], outcome.payload)["hotspots"][0]
        assert hotspot["candidates"][0]["title"] == "无名素材.mp4"


class TestRunAnalysisSummary:
    """同一条清单也要出现在 `run_hotspot_analysis` 的返回里：刚跑完就问「第 N 条」同样常见。"""

    @pytest.mark.asyncio
    async def test_summary_uses_the_same_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_load_run(_session: Any, _run_id: Any) -> Any:
            return run_detail()

        monkeypatch.setattr(chat_tools, "load_run", fake_load_run)

        summary = await chat_tools._summarise(context(tmp_path), RUN_UUID, "夜跑球鞋")

        assert summary["count"] == len(CANDIDATE_TITLES)
        assert [item["rank"] for item in summary["candidates"]] == [1, 2, 3, 4]
        assert summary["candidates"][0]["title"] == CANDIDATE_TITLES[0]
        assert summary["top1_title"] == CANDIDATE_TITLES[0]


class FakePollSession:
    """只满足 `_wait_for_run` 的形状（`expire_all` 不发 SQL）。"""

    def __init__(self) -> None:
        self.expired = 0

    def expire_all(self) -> None:
        self.expired += 1


class TestWaitForRunStages:
    """S7.20：轮询阶段——排队等待 / 素材同步 / 拆解逐段上报，同一阶段不重复。"""

    @pytest.mark.asyncio
    async def test_reports_new_stages_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs: list[dict[str, Any]] = [
            {"status": "queued", "progress": 0.0, "stage": None},
            {
                "status": "running",
                "progress": 0.05,
                "stage": {"stage": "material_sync", "status": "running"},
            },
            {
                "status": "running",
                "progress": 0.05,
                "stage": {"stage": "material_sync", "status": "running"},
            },
            {
                "status": "running",
                "progress": 0.4,
                "stage": {"stage": "clue", "status": "running"},
            },
            {
                "status": "succeeded",
                "progress": 1.0,
                "stage": {"stage": "report", "status": "succeeded"},
            },
        ]
        pending = list(jobs)

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return dict(pending.pop(0)) if pending else dict(jobs[-1])

        async def fake_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        session = FakePollSession()
        ctx = ToolContext(
            cfg=write_config(tmp_path),
            session=cast(Any, session),
            sleep=fake_sleep,
            poll_interval_s=0.0,
        )
        labels: list[str] = []
        updates: list[dict[str, Any]] = []

        async def progress(
            stage: str,
            done: int = 0,
            total: int = 0,
            *,
            update: dict[str, Any] | None = None,
        ) -> None:
            labels.append(stage)
            if update is not None:
                updates.append(dict(update))

        status = await chat_tools._wait_for_run(ctx, "job-1", progress)

        assert status == "succeeded"
        # R3：终态 job 的最后一份快照在返回前补发（报告阶段不再被丢掉）
        assert labels == ["排队等待", "素材同步", "分析中", "拆解", "报告"]
        assert session.expired == 5  # 每次轮询都先过期，读到的是库里的新值
        # 结构化更新只在内容变化时上报；同一阶段重复轮询不重复上报
        assert [item["stage_key"] for item in updates] == [
            "queue_wait",
            "material_sync",
            "clue",
            "report",
        ]
        assert updates[-1]["status"] == "succeeded"
        assert updates[0]["job_id"] == "job-1"


class TestWaitForRunStructuredUpdates:
    """R3（OBS-05）：结构化阶段更新——真实状态 / 源尝试号 / 重试 / 身份都不再丢失。"""

    def _ctx(self, tmp_path: Path, sleep: Any = None) -> Any:
        async def fake_sleep(_seconds: float) -> None:
            return None

        return ToolContext(
            cfg=write_config(tmp_path),
            session=cast(Any, FakePollSession()),
            sleep=sleep or fake_sleep,
            poll_interval_s=0.0,
        )

    @pytest.mark.asyncio
    async def test_forwards_real_status_retries_and_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jobs: list[dict[str, Any]] = [
            {
                "status": "running",
                "progress": 0.2,
                "run_id": "run-9",
                "job_id": "job-9",
                "stage": {
                    "stage": "clue",
                    "status": "failed",
                    "attempt": 1,
                    "retries": 2,
                    "error_category": "llm_error",
                },
            },
            {
                "status": "running",
                "progress": 0.3,
                "run_id": "run-9",
                "job_id": "job-9",
                "stage": {
                    "stage": "clue",
                    "status": "running",
                    "attempt": 2,
                    "retries": 2,
                },
            },
            {
                "status": "succeeded",
                "progress": 1.0,
                "run_id": "run-9",
                "job_id": "job-9",
                "stage": {
                    "stage": "report",
                    "status": "succeeded",
                    "attempt": 1,
                    "nodes": [
                        {
                            "stage": "report",
                            "status": "succeeded",
                            "attempt": 1,
                            "latency_ms": 7,
                        }
                    ],
                },
            },
        ]
        pending = list(jobs)

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return dict(pending.pop(0)) if pending else dict(jobs[-1])

        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        updates: list[dict[str, Any]] = []

        async def progress(
            stage: str,
            done: int = 0,
            total: int = 0,
            *,
            update: dict[str, Any] | None = None,
        ) -> None:
            if update is not None:
                updates.append(dict(update))

        status = await chat_tools._wait_for_run(self._ctx(tmp_path), "job-9", progress)

        assert status == "succeeded"
        failed = updates[0]
        assert (
            failed["stage_key"],
            failed["status"],
            failed["attempt"],
            failed["retries"],
            failed["error_category"],
        ) == ("clue", "failed", 1, 2, "llm_error")
        assert failed["run_id"] == "run-9" and failed["job_id"] == "job-9"
        retry = updates[1]
        assert retry["stage_key"] == "clue" and retry["status"] == "running"
        assert retry["attempt"] == 2  # 重试：更高的源尝试号
        final = updates[2]
        assert final["status"] == "succeeded" and final["latency_ms"] == 7

    @pytest.mark.asyncio
    async def test_skipped_stage_is_forwarded_as_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job = {
            "status": "succeeded",
            "progress": 1.0,
            "run_id": "run-1",
            "stage": {"stage": "clue", "status": "skipped", "attempt": 1},
        }

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return dict(job)

        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        updates: list[dict[str, Any]] = []

        async def progress(
            stage: str,
            done: int = 0,
            total: int = 0,
            *,
            update: dict[str, Any] | None = None,
        ) -> None:
            if update is not None:
                updates.append(dict(update))

        status = await chat_tools._wait_for_run(self._ctx(tmp_path), "job-1", progress)

        assert status == "succeeded"
        assert [item["status"] for item in updates] == ["skipped"]

    @pytest.mark.asyncio
    async def test_queued_maps_to_queue_wait_without_fabricating_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pending = [
            {"status": "queued", "progress": 0.0, "stage": {}},
            {"status": "succeeded", "progress": 1.0, "stage": {}},
        ]

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return (
                dict(pending.pop(0))
                if pending
                else {"status": "succeeded", "stage": {}}
            )

        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        updates: list[dict[str, Any]] = []

        async def progress(
            stage: str,
            done: int = 0,
            total: int = 0,
            *,
            update: dict[str, Any] | None = None,
        ) -> None:
            if update is not None:
                updates.append(dict(update))

        status = await chat_tools._wait_for_run(self._ctx(tmp_path), "job-1", progress)

        assert status == "succeeded"
        assert updates == [
            {
                "stage_key": "queue_wait",
                "label": "排队等待",
                "status": "running",
                "attempt": 1,
                "retries": 0,
                "error_category": "",
                "latency_ms": 0,
                "run_id": "",
                "job_id": "job-1",
            }
        ]

    def test_update_marker_includes_source_identity_and_terminal_latency(self) -> None:
        """R5（E7OBS2-03）：去重键带来源身份——不同来源不许静默去重；终态耗时修订要能到达。"""
        base = {
            "stage_key": "clue",
            "status": "succeeded",
            "attempt": 1,
            "retries": 0,
            "error_category": "",
            "run_id": "run-a",
            "job_id": "job-1",
            "latency_ms": 5,
        }
        marker = chat_tools._update_marker(base)
        assert chat_tools._update_marker(dict(base)) == marker  # 同内容去重
        assert (
            chat_tools._update_marker({**base, "run_id": "run-b"}) != marker
        )  # 换 run：不是同一事实
        assert chat_tools._update_marker({**base, "job_id": "job-2"}) != marker
        assert (
            chat_tools._update_marker({**base, "latency_ms": 9}) != marker
        )  # 终态耗时修订
        running = {**base, "status": "running", "latency_ms": 0}
        assert chat_tools._update_marker(running) == chat_tools._update_marker(
            {**running, "latency_ms": 7}
        )  # 运行中不带耗时：不逐轮询上报

    @pytest.mark.asyncio
    async def test_terminal_latency_revision_reaches_the_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """终态节点的耗时随后修正（0 → 9）：修正那次也要上报，不能被去重吞掉。"""
        jobs = [
            {
                "status": "running",
                "progress": 0.5,
                "run_id": "run-1",
                "job_id": "job-1",
                "stage": {
                    "stage": "clue",
                    "status": "succeeded",
                    "attempt": 1,
                    "nodes": [
                        {
                            "stage": "clue",
                            "status": "succeeded",
                            "attempt": 1,
                            "latency_ms": 0,
                        }
                    ],
                },
            },
            {
                "status": "running",
                "progress": 0.5,
                "run_id": "run-1",
                "job_id": "job-1",
                "stage": {
                    "stage": "clue",
                    "status": "succeeded",
                    "attempt": 1,
                    "nodes": [
                        {
                            "stage": "clue",
                            "status": "succeeded",
                            "attempt": 1,
                            "latency_ms": 9,
                        }
                    ],
                },
            },
            {
                "status": "succeeded",
                "progress": 1.0,
                "run_id": "run-1",
                "job_id": "job-1",
                "stage": {"stage": "report", "status": "succeeded", "attempt": 1},
            },
        ]
        pending = list(jobs)

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return dict(pending.pop(0)) if pending else dict(jobs[-1])

        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        updates: list[dict[str, Any]] = []

        async def progress(
            stage: str,
            done: int = 0,
            total: int = 0,
            *,
            update: dict[str, Any] | None = None,
        ) -> None:
            if update is not None:
                updates.append(dict(update))

        status = await chat_tools._wait_for_run(self._ctx(tmp_path), "job-1", progress)

        assert status == "succeeded"
        clue_updates = [item for item in updates if item["stage_key"] == "clue"]
        assert [item["latency_ms"] for item in clue_updates] == [0, 9]  # 修订到达


class TestToolClosureForwardsStructuredUpdate:
    """R6 冒烟回归（OBS-12）：run_hotspot_analysis 的 progress 闭包必须把结构化
    `update` 透传给 on_progress——否则工具层一开跑就 TypeError（真机冒烟抓到的缺陷）。"""

    @pytest.mark.asyncio
    async def test_run_hotspot_analysis_forwards_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        submission = SimpleNamespace(job_id="job-1", run_id="run-1")

        async def fake_submit(_session: Any, _hotspots: Any, **_kw: Any) -> Any:
            return submission

        jobs: list[dict[str, Any]] = [
            {"status": "queued", "progress": 0.0, "stage": None},
            {
                "status": "running",
                "progress": 0.4,
                "run_id": "run-1",
                "job_id": "job-1",
                "stage": {
                    "stage": "clue",
                    "status": "running",
                    "attempt": 1,
                    "retries": 0,
                },
            },
            {
                "status": "succeeded",
                "progress": 1.0,
                "run_id": "run-1",
                "job_id": "job-1",
                "stage": {"stage": "report", "status": "succeeded", "attempt": 1},
            },
        ]
        pending = list(jobs)

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return dict(pending.pop(0)) if pending else dict(jobs[-1])

        async def fake_summarise(
            _ctx: Any, _run_id: str, _hotspot: str
        ) -> dict[str, Any]:
            return {"cost_cny": 0.0, "prompt_tokens": 0, "completion_tokens": 0}

        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)
        monkeypatch.setattr(chat_tools, "_summarise", fake_summarise)

        class _Session(FakePollSession):
            pass

        ctx = ToolContext(
            cfg=write_config(tmp_path),
            session=cast(Any, _Session()),
            poll_interval_s=0.0,
        )
        updates: list[dict[str, Any]] = []

        async def spy(
            stage: str,
            done: int = 0,
            total: int = 0,
            *,
            update: dict[str, Any] | None = None,
        ) -> None:
            if update is not None:
                updates.append(dict(update))

        outcome = await chat_tools.run_hotspot_analysis(
            ctx, {"hotspot": "周末爬山"}, on_progress=spy
        )

        assert outcome.status == "succeeded"
        # 闭包把 _wait_for_run 的结构化快照原样带到 on_progress（不再 TypeError）
        assert [item["stage_key"] for item in updates] == [
            "queue_wait",
            "clue",
            "report",
        ]
        assert updates[-1]["status"] == "succeeded"


class TestPartialConsumptionOnFailure:
    """R5（E7OBS2-01）：工具内部部分成功后失败，已知消费与运行引用不丢、未知单列。"""

    def _parsed_image(self) -> ImageClueParse:
        clue = HotspotClue.from_dict({**CLUE_PAYLOAD, "hotspot_raw": "夜跑球鞋开箱"})
        return ImageClueParse(
            raw_text="夜跑球鞋开箱",
            clue=clue,
            prompt_versions={"image_hotspot_clue": 1},
            prompt_tokens=500,
            completion_tokens=20,
            cost_cny=0.003,
        )

    @pytest.mark.asyncio
    async def test_image_cost_survives_analysis_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """图片解析已成功、分析超时：视觉已知账目与 run 引用保留，未知单独标注。"""

        async def fake_image(_ctx: Any, _group: Any, *, force_reparse: bool = False, **kwargs: Any) -> ImageClueParse:
            return self._parsed_image()

        async def fake_submit(*_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(job_id="job-1", run_id=RUN_UUID)

        async def fake_load_job(_session: Any, _job_id: str) -> dict[str, Any]:
            return {"status": "running", "progress": 0.1, "stage": {}}

        monkeypatch.setattr(chat_tools, "_image_clue_for", fake_image)
        monkeypatch.setattr(chat_tools, "submit_analysis", fake_submit)
        monkeypatch.setattr(chat_tools, "load_job", fake_load_job)

        ctx = ToolContext(
            cfg=write_config(tmp_path),
            session=cast(Any, FakePollSession()),
            poll_interval_s=0.0,
            max_wait_s=0.0,
        )
        outcome = await chat_tools.run_hotspot_analysis(
            ctx, {"hotspot": ""}, attachments=[{"path": "a.png"}]
        )

        assert outcome.status == "failed" and "超过等待上限" in outcome.error
        assert outcome.payload["usage_status"] == "unknown"  # 分析那段未知
        assert outcome.payload["cost_cny"] == 0.003  # 图片那段已知，不丢
        assert outcome.payload["prompt_tokens"] == 500
        assert outcome.payload["completion_tokens"] == 20
        assert outcome.payload["run_id"] == RUN_UUID  # 运行引用保留
        assert outcome.prompt_versions == {"image_hotspot_clue": 1}

    @pytest.mark.asyncio
    async def test_clue_cost_survives_retrieval_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拆解已成功、检索失败：拆解的已知消费与版本保留，检索那段未知单列。"""
        record = chat_tools.ClueRecord(
            clue=HotspotClue.from_dict({**CLUE_PAYLOAD, "hotspot_raw": "夜跑球鞋开箱"}),
            version=2,
            cost_cny=0.004,
            prompt_tokens=300,
            completion_tokens=100,
        )

        async def fake_clue(_ctx: Any, _text: str) -> Any:
            return record

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise LLMError("检索挂了")

        monkeypatch.setattr(chat_tools, "_clue_for", fake_clue)
        monkeypatch.setattr(chat_tools, "retrieve_candidates", boom)

        outcome = await chat_tools.search_materials(
            context(tmp_path), {"hotspot": "夜跑球鞋开箱"}
        )

        assert outcome.status == "failed" and "检索素材失败" in outcome.error
        assert outcome.payload["usage_status"] == "unknown"
        assert outcome.payload["cost_cny"] == 0.004
        assert outcome.payload["prompt_tokens"] == 300
        assert outcome.prompt_versions == {chat_tools.CLUE_TASK: 2}


class TestUsageStatusAggregation:
    """E7OBS4-05（E5 / E6）：窗口内的用量来源按事实汇总，缺来源不被 actual 掩盖。"""

    def _caller(self, records: list[dict[str, Any]]) -> Any:
        caller = SimpleNamespace(records=records)
        return caller

    def test_missing_source_beside_actual_is_unknown(self) -> None:
        from xhs_agent.services.chat_tools import _records_usage_status

        caller = self._caller([
            {"cost_cny": 0.001, "prompt_tokens": 10, "completion_tokens": 5,
             "usage_status": "actual"},
            {"cost_cny": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
             "usage_status": ""},
        ])
        assert _records_usage_status(caller, 0) == "unknown"   # 零值也算调用事实

    def test_status_precedence(self) -> None:
        from xhs_agent.services.chat_tools import _records_usage_status

        def status_of(*values: str) -> str:
            caller = self._caller([{"usage_status": item} for item in values])
            return _records_usage_status(caller, 0)

        assert status_of() == ""                                  # 空窗口
        assert status_of("cached") == "cached"
        assert status_of("cached", "cached") == "cached"
        assert status_of("actual", "cached") == "actual"          # 缓存不额外算调用
        assert status_of("actual", "estimated") == "estimated"    # 不冒充 actual
        assert status_of("actual", "unknown") == "unknown"
        assert status_of("estimated", "cached") == "estimated"

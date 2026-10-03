"""三个 agent 的单测：用假 provider + 真 prompt 资产，离线、不联网（S3.1 / S3.5）。"""

from __future__ import annotations

import json

import pytest

from xhs_agent.agents import explain_candidates, extract_clue, write_draft
from xhs_agent.schemas import MatchCandidate, Material
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMError, LLMResult, StructuredCaller

MATERIAL_ID = "m_test_1"

CLUE_PAYLOAD = {
    "hotspot_raw": "某明星打羽毛球（模型改写版，应被输入覆盖）",
    "why_it_works": ["反差：明星身份 vs 业余球场"],
    "mechanisms": [{"name": "反差", "explain": "身份反差"}],
    "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9,
                  "evidence": "热点原文片段"}],
    "match_keywords": ["羽毛球", "球场"],
    "borrow_angles": ["同款球场热场"],
    "risk_notes": ["别用明星肖像"],
}

EXPLAIN_PAYLOAD = {"candidates": [
    {"material_id": MATERIAL_ID, "rank": 1,
     "reasons": ["模型理由：命中主题「羽毛球」"], "usage": "模型用法：放开头 3 秒"},
]}

DRAFT_PAYLOAD = {
    "titles": [{"text": "球场热身也能出片", "style": "直给"}],
    "cover_text": "球场热身",
    "first_3s": "开场给挥拍特写",
    "body": "最近羽毛球刷屏，我用自己的球场素材试了一遍。",
    "tags": ["羽毛球", "运动"],
    "shot_list": ["先拍球场空镜"],
    "compliance_notes": ["别用明星肖像"],
}


class FakeProvider(llm.BaseProvider):
    """按任务返回预置 JSON；`bad_json=True` 时返回不可解析文本（S3.5 起 `complete` 是协程）。"""

    name = "fake"

    def __init__(self, *, bad_json: bool = False, error: str = "") -> None:
        super().__init__(model="fake-1")
        self.bad_json = bad_json
        self.error = error
        self.tasks: list[str] = []

    async def complete(self, call):
        self.tasks.append(call.task)
        if self.error:
            return LLMResult(text="", provider=self.name, model=self.model, error=self.error)
        if self.bad_json:
            return LLMResult(text="这不是 JSON", provider=self.name, model=self.model)
        payload = {"hotspot_clue": CLUE_PAYLOAD, "material_select": EXPLAIN_PAYLOAD,
                   "copy_draft": DRAFT_PAYLOAD}[call.task]
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50)


def caller(**kwargs) -> StructuredCaller:
    return StructuredCaller(provider=FakeProvider(**kwargs))


def material() -> Material:
    return Material(id=MATERIAL_ID, path="D:/materials/球场热身.mp4", title="球场热身",
                    tags=["羽毛球"], width=1080, height=1920, duration_s=15)


def candidate() -> MatchCandidate:
    return MatchCandidate(material_id=MATERIAL_ID, material=material(), score=0.8, rank=1,
                          recall_sources=["literal"], reasons=["规则理由"], usage="规则用法")


class TestExtractClue:
    @pytest.mark.asyncio
    async def test_returns_validated_clue_with_version(self):
        outcome = await extract_clue("某明星打羽毛球", caller())
        assert outcome.task_id == "hotspot_clue"
        assert outcome.version == 1
        assert outcome.value.hotspot_raw == "某明星打羽毛球"  # 用输入，不信模型改写
        assert outcome.value.element_values == ["羽毛球"]
        assert outcome.value.provider == "fake" and outcome.value.model == "fake-1"

    @pytest.mark.asyncio
    async def test_provider_error_raises_without_repair(self):
        with pytest.raises(LLMError):
            await extract_clue("热点", caller(error="上游 503"))

    @pytest.mark.asyncio
    async def test_two_bad_json_attempts_raise(self):
        with pytest.raises(LLMError):
            await extract_clue("热点", caller(bad_json=True))


class TestExplainCandidates:
    @pytest.mark.asyncio
    async def test_model_reasons_override_rule_reasons(self):
        clue = (await extract_clue("热点", caller())).value
        outcome = await explain_candidates(clue, [candidate()], caller())
        updated = outcome.value[0]
        assert outcome.version == 1
        assert updated.reasons == ["模型理由：命中主题「羽毛球」"]
        assert updated.usage == "模型用法：放开头 3 秒"
        assert updated.rank == 1                      # 排序不动
        assert updated.finalize() is updated          # 仍是契约终态

    @pytest.mark.asyncio
    async def test_without_candidates_no_model_call(self):
        fake = FakeProvider()
        clue = (await extract_clue("热点", caller())).value
        outcome = await explain_candidates(clue, [], StructuredCaller(provider=fake))
        assert outcome.value == []
        assert outcome.version is None
        assert fake.tasks == []

    @pytest.mark.asyncio
    async def test_missing_entry_is_rejected(self):
        class Partial(FakeProvider):
            async def complete(self, call):
                if call.task == "material_select":
                    return LLMResult(text=json.dumps({"candidates": []}), provider=self.name,
                                     model=self.model)
                return await super().complete(call)

        clue = (await extract_clue("热点", caller())).value
        with pytest.raises(LLMError):
            await explain_candidates(clue, [candidate()], StructuredCaller(provider=Partial()))

    @pytest.mark.asyncio
    async def test_missing_reasons_is_rejected(self):
        class NoReasons(FakeProvider):
            async def complete(self, call):
                if call.task == "material_select":
                    payload = {"candidates": [{"material_id": MATERIAL_ID, "usage": "放开头"}]}
                    return LLMResult(text=json.dumps(payload), provider=self.name,
                                     model=self.model)
                return await super().complete(call)

        clue = (await extract_clue("热点", caller())).value
        with pytest.raises(LLMError):
            await explain_candidates(clue, [candidate()],
                                     StructuredCaller(provider=NoReasons()))

    @pytest.mark.asyncio
    async def test_unknown_material_is_rejected(self):
        class Ghost(FakeProvider):
            async def complete(self, call):
                if call.task == "material_select":
                    payload = {"candidates": [
                        {"material_id": "m_ghost", "reasons": ["r"], "usage": "u"},
                        *EXPLAIN_PAYLOAD["candidates"]]}
                    return LLMResult(text=json.dumps(payload), provider=self.name,
                                     model=self.model)
                return await super().complete(call)

        clue = (await extract_clue("热点", caller())).value
        with pytest.raises(LLMError):
            await explain_candidates(clue, [candidate()], StructuredCaller(provider=Ghost()))


class TestWriteDraft:
    @pytest.mark.asyncio
    async def test_returns_draft_with_inputs_attached(self):
        clue = (await extract_clue("热点", caller())).value
        outcome = await write_draft(clue, material().to_dict(), caller())
        draft = outcome.value
        assert outcome.version == 1
        assert draft.material_id == MATERIAL_ID
        assert draft.hotspot_key == clue.hotspot_key
        assert draft.body.startswith("最近羽毛球刷屏")
        assert draft.tags == ["#羽毛球", "#运动"]      # 契约会补 #
        assert draft.provider == "fake"

    @pytest.mark.asyncio
    async def test_without_material_no_model_call(self):
        fake = FakeProvider()
        clue = (await extract_clue("热点", caller())).value
        outcome = await write_draft(clue, None, StructuredCaller(provider=fake))
        assert outcome.value is None
        assert outcome.version is None
        assert fake.tasks == []

    @pytest.mark.asyncio
    async def test_missing_body_is_rejected(self):
        class NoBody(FakeProvider):
            async def complete(self, call):
                if call.task == "copy_draft":
                    return LLMResult(text=json.dumps({"titles": []}), provider=self.name,
                                     model=self.model)
                return await super().complete(call)

        clue = (await extract_clue("热点", caller())).value
        with pytest.raises(LLMError):
            await write_draft(clue, material().to_dict(), StructuredCaller(provider=NoBody()))

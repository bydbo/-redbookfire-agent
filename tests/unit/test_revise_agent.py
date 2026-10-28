"""`copy_revise` 单点智能体的离线单测（S7.8）：两种输出形状的解析与校验。

工具层（参数校验、run 读取、成本与版本）在 `test_chat_tools.py`；这里只管"模型给回来的
那两种 JSON 怎么被收下或拒收"。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from xhs_agent.agents.revise import TASK_ID, parse_revise, revise_draft
from xhs_agent.schemas import HotspotClue, SchemaError
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult, StructuredCaller


class ScriptedProvider(llm.BaseProvider):
    """按剧本回一段 JSON 文本（不联网）。"""

    name = "scripted"

    def __init__(self, payload: Any) -> None:
        super().__init__(model="scripted-1")
        self.payload = payload
        self.calls = 0

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        self.calls += 1
        return LLMResult(text=json.dumps(self.payload, ensure_ascii=False),
                         provider=self.name, model=self.model,
                         prompt_tokens=10, completion_tokens=5, cost_cny=0.004)


def clue() -> HotspotClue:
    return HotspotClue.from_dict({
        "hotspot_raw": "夜跑球鞋开箱",
        "why_it_works": ["反差"],
        "mechanisms": [{"name": "反差", "explain": "夜里跑步"}],
        "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9, "confidence": 0.9,
                      "evidence": "原文"}],
        "match_keywords": ["夜跑"],
    })


DRAFT = {"material_id": "m-1", "hotspot_key": "夜跑球鞋开箱",
         "titles": [{"text": "原标题", "style": "直给"}], "body": "原始正文",
         "tags": ["#夜跑"], "cover_text": "", "first_3s": "", "shot_list": [],
         "compliance_notes": []}


class TestParseRevise:
    def test_accepts_a_rewritten_draft(self) -> None:
        outcome = parse_revise(
            {"refused": False, "draft": {"titles": [{"text": "改后标题"}],
                                         "body": "改后的正文", "tags": ["#夜跑"]}},
            material_id="m-1", hotspot_key="夜跑球鞋开箱")
        assert outcome.refused is False
        assert outcome.draft is not None and outcome.draft.body == "改后的正文"
        assert outcome.draft.material_id == "m-1"          # 关联字段由服务端写入
        assert outcome.draft.hotspot_key == "夜跑球鞋开箱"
        assert outcome.draft.provider == ""                # provider / model 由调用方注入

    def test_accepts_a_refusal_with_a_reason(self) -> None:
        outcome = parse_revise({"refused": True, "reason": "不能用他人的姓名与肖像"})
        assert outcome.refused is True and outcome.draft is None
        assert "肖像" in outcome.reason

    @pytest.mark.parametrize("payload", [
        "not-a-dict",
        {"refused": True},                                   # 拒绝但没给理由
        {"refused": True, "reason": "   "},
        {"refused": False},                                  # 没说拒绝也没给初稿
        {"refused": False, "draft": {"body": "   "}},        # 初稿没有正文
    ])
    def test_rejects_malformed_payloads(self, payload: Any) -> None:
        with pytest.raises(SchemaError):
            parse_revise(payload)

    def test_rejects_model_rebinding_the_material(self) -> None:
        """S6.11b：素材身份由基础文案决定，模型不能把改稿绑到另一个素材。"""
        with pytest.raises(SchemaError, match="material_id"):
            parse_revise(
                {"refused": False,
                 "draft": {"material_id": "m-other", "body": "改后的正文"}},
                material_id="m-1", hotspot_key="夜跑球鞋开箱")

    def test_accepts_matching_material_id(self) -> None:
        outcome = parse_revise(
            {"refused": False, "draft": {"material_id": "m-1", "body": "改后的正文"}},
            material_id="m-1")
        assert outcome.draft is not None and outcome.draft.material_id == "m-1"


class TestReviseAgent:
    @pytest.mark.asyncio
    async def test_renders_prompt_and_returns_the_outcome(self) -> None:
        provider = ScriptedProvider({"refused": False, "draft": {"body": "改后的正文"}})
        outcome, record = await _run(provider, request="正文写短一点")

        assert outcome.value.refused is False
        assert outcome.value.draft.body == "改后的正文"
        assert outcome.value.draft.provider == provider.name       # 记账字段由调用方注入
        assert outcome.task_id == TASK_ID and outcome.version == 3
        assert record["task"] == TASK_ID
        assert record["cost_cny"] == pytest.approx(0.004)

    @pytest.mark.asyncio
    async def test_refusal_keeps_the_reason_and_no_draft(self) -> None:
        provider = ScriptedProvider({"refused": True, "reason": "不能编造素材里没有的事"})
        outcome, _model = await _run(provider, request="编一个我去了巴黎的桥段")

        assert outcome.value.refused is True
        assert outcome.value.draft is None
        assert "编造" in outcome.value.reason


async def _run(provider: ScriptedProvider, *, request: str):
    caller = StructuredCaller(provider=provider)
    outcome = await revise_draft(clue(), {"id": "m-1", "title": "夜里的球鞋"},
                                 DRAFT, request, caller)
    return outcome, caller.records[-1]

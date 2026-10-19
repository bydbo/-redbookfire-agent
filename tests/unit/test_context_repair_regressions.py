"""E7 第六批修复的**单元复现**（v2 §4.1.1，case_id 对应问题清单）。

每个用例先固化"修复前的缺陷行为"（注释里注明），再断言修复后的正确结果。
需要真库 / HTTP 的场景见 `tests/integration/test_context_repair_http.py`。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from xhs_agent.core import context_rules as rules
from xhs_agent.core.budget import BudgetError, decide
from xhs_agent.schemas import ContextDelta, ContextSnapshot
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult

TOPIC = "topic:t1"


def delta(operations: list[dict[str, object]] | None = None,
          proposals: list[dict[str, object]] | None = None) -> ContextDelta:
    return ContextDelta.from_dict({"operations": operations or [],
                                   "proposals": proposals or []})


def apply(snapshot: ContextSnapshot, data: ContextDelta, *, user_text: str,
          version: int = 1) -> rules.DeltaResult:
    return rules.apply_delta(
        snapshot, data, user_text=user_text, message_id="m-1",
        version=version, topic_id=snapshot.topic_id or TOPIC,
    )


class TestCTX01RetractCountsAsChange:
    """缺陷：撤回返回 retracted 却不计为变更，旧约束继续生效。"""

    def test_single_retract_advances_and_persists_in_snapshot(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC),
                      delta([{"op": "add", "kind": "constraint", "value": "300 字以内",
                              "evidence": "300 字以内"}]),
                      user_text="控制在 300 字以内")
        fact_id = first.snapshot.facts[0].fact_id
        second = apply(first.snapshot,
                       delta([{"op": "retract", "target_fact_id": fact_id,
                               "evidence": "撤回字数限制"}]),
                       user_text="撤回字数限制", version=2)
        assert second.changed is True            # 缺陷时为 False
        assert second.retracted == 1
        assert second.snapshot.facts[0].status == "retracted"

    def test_repeat_retract_is_noop_not_failure(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC),
                      delta([{"op": "add", "kind": "constraint", "value": "300 字以内",
                              "evidence": "300 字以内"}]),
                      user_text="控制在 300 字以内")
        fact_id = first.snapshot.facts[0].fact_id
        retract = delta([{"op": "retract", "target_fact_id": fact_id,
                          "evidence": "撤回字数限制"}])
        once = apply(first.snapshot, retract, user_text="撤回字数限制", version=2)
        twice = apply(once.snapshot, retract, user_text="撤回字数限制", version=3)
        assert twice.changed is False             # 重复撤回不推进版本
        assert twice.invalid is False             # 也不算批失败
        assert twice.decisions[0]["decision"] == "noop"


class TestCTX02NoTruncation:
    """缺陷：状态装配前 12/20 条截断、40 条自动归档有效事实。"""

    def test_render_state_includes_all_41_facts(self) -> None:
        snapshot = ContextSnapshot(topic_id=TOPIC)
        operations = [
            {"op": "add", "kind": "constraint", "value": f"第{i}条限制，字数上限 {i}",
             "evidence": f"第{i}条限制"}
            for i in range(41)
        ]
        result = apply(snapshot, delta(operations),
                       user_text="，".join(f"第{i}条限制" for i in range(41)))
        assert result.applied == 41
        rendered = json.loads(rules.render_state(result.snapshot))
        assert len(rendered) == 41                # 缺陷时最多 20 条
        values = {item["value"] for item in rendered}
        assert "第0条限制，字数上限 0" in values and "第40条限制，字数上限 40" in values

    def test_state_prompt_contains_all_active_facts(self) -> None:
        from xhs_agent.services.context_state import StoredContext, state_prompt

        snapshot = ContextSnapshot(topic_id=TOPIC)
        for i in range(21):
            snapshot.facts.append(
                rules.ContextFact(  # type: ignore[attr-defined]
                    fact_id=f"f{i}", kind="constraint", value=f"限制{i}",
                    scope="topic", scope_id=TOPIC, status="active",
                    evidence="限制", source_ref={"message_id": "m"},
                    created_version=1,
                )
            )
        stored = StoredContext(session_id=__import__("uuid").uuid4(), state_version=1,
                               topic_id=TOPIC, snapshot=snapshot)
        prompt = state_prompt(stored)
        for i in range(21):
            assert f"限制{i}" in prompt            # 缺陷时只装 12 条


class TestCTX03LegacyConflictBlocks:
    """缺陷：遗留冲突下一轮不再阻断；CAS 失败继续执行。"""

    def test_legacy_conflict_is_blocking_regardless_of_created_version(self) -> None:
        snapshot = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta(proposals=[{"kind": "goal", "value": "换一个",
                              "reason": "ambiguous_reference"}]),
            user_text="换一个", version=2,
        ).snapshot
        # 不传 created_version：遗留冲突也要被检出（缺陷时按本轮新增过滤而漏检）
        assert rules.blocking_proposals(snapshot)
        assert rules.blocking_proposals(snapshot, created_version=2)


class TestCTX04ConfirmReplaceSupersedes:
    """缺陷：确认冲突提案只新增事实，新旧限制同时有效。"""

    def _with_conflict(self) -> tuple[ContextSnapshot, str, str]:
        first = apply(ContextSnapshot(topic_id=TOPIC),
                      delta([{"op": "add", "kind": "constraint", "value": "300 字以内",
                              "evidence": "300 字以内"}]),
                      user_text="300 字以内")
        old_id = first.snapshot.facts[0].fact_id
        second = apply(first.snapshot,
                       delta([{"op": "add", "kind": "constraint",
                               "value": "100 字以内", "evidence": "100 字以内"}]),
                       user_text="改成 100 字以内", version=2)
        proposal = second.snapshot.proposals[0]
        assert proposal.operation == "replace"
        assert proposal.replaces_fact_ids == [old_id]
        return second.snapshot, proposal.proposal_id, old_id

    def test_confirm_supersedes_old_fact_atomically(self) -> None:
        snapshot, proposal_id, old_id = self._with_conflict()
        acted = rules.apply_action(snapshot, action="confirm", target_id=proposal_id,
                                   topic_id=TOPIC, version=3)
        assert acted.decision == "applied"
        statuses = {fact.fact_id: fact.status for fact in acted.snapshot.facts}
        assert statuses[old_id] == "superseded"   # 缺陷时仍是 active
        new_fact = next(f for f in acted.snapshot.facts if f.status == "active")
        assert new_fact.value == "100 字以内"
        assert new_fact.replaces_fact_ids == [old_id]

    def test_confirm_with_changed_target_is_conflict(self) -> None:
        snapshot, proposal_id, old_id = self._with_conflict()
        # 旧项已被撤回：确认替换必须返回 conflict（服务层折 409），不产生部分修改
        broken = snapshot.model_copy(deep=True)
        for fact in broken.facts:
            if fact.fact_id == old_id:
                fact.status = "retracted"
        acted = rules.apply_action(broken, action="confirm", target_id=proposal_id,
                                   topic_id=TOPIC, version=3)
        assert acted.decision == "conflict"
        # 原快照未被半改：旧项保持 retracted、没有新事实落进来
        assert all(f.status != "active" or f.value == "300 字以内"
                   for f in acted.snapshot.facts)


class TestCTX09BeforeRequestBudget:
    """缺陷：改稿未接预算入口；抽取结构修复未重新检查预算。"""

    @pytest.mark.asyncio
    async def test_before_request_runs_for_every_call(self) -> None:
        calls: list[str] = []
        blocked: list[str] = []

        async def before_request(call: llm.LLMCall) -> None:
            calls.append(call.task)
            if len(calls) >= 2:
                blocked.append(call.task)
                raise BudgetError("第二次请求超限", reason="required_context_overflow")

        caller = llm.StructuredCaller(provider=BrokenIfSecondProvider(), before_request=before_request)
        with pytest.raises(BudgetError):
            await caller.call("t", "s", "u", parse=lambda x: x)
        assert calls == ["t", "t"]        # 初始请求与修复请求**各检查一次**

    @pytest.mark.asyncio
    async def test_budget_uses_actual_output_reserve(self) -> None:
        decision = decide(
            messages=[{"role": "user", "content": "x" * 3000}],
            tools=None,
            output_reserve=16000,
            application_limit=1024,
            mode="enforce",
        )
        assert decision.allowed is False
        assert decision.reason == "required_context_overflow"


class BrokenIfSecondProvider(llm.BaseProvider):
    name = "broken-second"

    def __init__(self) -> None:
        super().__init__(model="broken-1")
        self.calls = 0

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        # 第一次请求放行（返回坏 JSON 触发修复），第二次应在 before_request 被拦截
        return LLMResult(text="not-json", provider=self.name, model=self.model)

    async def complete_with_tools(self, call: llm.LLMCall, on_delta: Any = None) -> LLMResult:
        return await self.complete(call)


class TestCTX17ObjectRefValidation:
    """缺陷：对象选择 / 作用域未形成服务端闭环（纯层部分：作用域与引用解析）。"""

    def test_artifact_scope_requires_verified_ref(self) -> None:
        result = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([{"op": "add", "kind": "constraint", "value": "封面别用红色",
                    "evidence": "封面别用红色", "scope": "artifact",
                    "scope_id": "draft:r1:1"}]),
            user_text="封面别用红色",
        )
        # 未验证的产物作用域不得激活，转为提案等待选择
        assert result.decisions[0]["decision"] == "pending_confirmation"
        assert result.decisions[0]["reason"] == "scope_unresolved"

    def test_select_object_stores_structured_ref(self) -> None:
        snapshot = ContextSnapshot(topic_id=TOPIC)
        acted = rules.apply_action(
            snapshot, action="select_object",
            value=json.dumps({"object_key": "draft:r1:1", "object_type": "draft"}),
            topic_id=TOPIC, version=1,
        )
        assert acted.decision == "applied"
        assert acted.snapshot.object_refs[0]["object_key"] == "draft:r1:1"

    def test_reset_topic_keeps_session_constraints(self) -> None:
        snapshot = ContextSnapshot(topic_id=TOPIC)
        snapshot.object_refs = [{"object_key": "draft:r1:1", "object_type": "draft"}]
        with_session = apply(
            snapshot,
            delta([{"op": "add", "kind": "constraint", "value": "全对话不用感叹号",
                    "evidence": "全对话不用感叹号", "scope": "session"}]),
            user_text="全对话不用感叹号",
        )
        with_topic = apply(
            with_session.snapshot,
            delta([{"op": "add", "kind": "goal", "value": "蹭热点", "evidence": "蹭热点"}]),
            user_text="蹭热点", version=2,
        )
        reset = rules.apply_action(with_topic.snapshot, action="reset_topic",
                                   topic_id=TOPIC, version=3)
        active = [fact.value for fact in reset.snapshot.active()]
        assert active == ["全对话不用感叹号"]     # 会话级限制存活，话题项归档
        assert reset.snapshot.object_refs == []


# ---------------------------------------------------------------------------
# 二次验收 A01–A13 的固定回归（需要真库 / HTTP 的部分见 integration）
# ---------------------------------------------------------------------------


class TestA01RecoveryBaseline:
    """A01：新建会话写入明确维护起点；坏恢复字段不得无声丢弃后假定完整。"""

    def test_new_recovery_is_complete_with_baseline(self) -> None:
        recovery = rules.new_recovery(
            baseline_created_at="2026-10-18T00:00:00+00:00",
            baseline_message_id="m-1",
        )
        assert recovery["status"] == "complete"      # 新空会话首轮不要求先恢复
        assert recovery["baseline_created_at"]
        assert recovery["baseline_message_id"] == "m-1"

    def test_corrupt_recovery_degrades_to_pending(self) -> None:
        parsed = rules.recovery_of({"recovery": {"status": "gone", "recovery_id": "r1"}})
        assert parsed["status"] == "pending"         # 不假定完整
        assert parsed["recovery_id"] == "r1"         # 原有字段不无声丢弃

    def test_missing_recovery_is_empty(self) -> None:
        assert rules.recovery_of({}) == {}
        assert rules.recovery_of({"recovery": {}}) == {}


class TestA02EventTypes:
    """A02：审计事件约束必须放行 select_object / complete_recovery。"""

    def test_model_constraint_allows_new_events(self) -> None:
        from xhs_agent.db.models import ChatContextEvent

        constraints = [
            str(item.sqltext) for item in ChatContextEvent.__table__.constraints
            if str(getattr(item, "name", "")).endswith("event_type_allowed")
        ]
        assert constraints, "chat_context_events 必须保留 event_type_allowed 约束"
        text = constraints[0]
        assert "select_object" in text and "complete_recovery" in text
        # 旧合法事件不能丢
        for legacy in ("delta", "confirm", "reject", "retract", "replace",
                       "reset_topic", "recovery"):
            assert legacy in text


class TestA05ApplicableFacts:
    """A05：适用事实按作用域筛选，其他话题 / 其他对象 / 失效项不得混入。"""

    def _snapshot(self) -> ContextSnapshot:
        snapshot = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([
                {"op": "add", "kind": "constraint", "value": "全对话不用感叹号",
                 "evidence": "全对话不用感叹号", "scope": "session"},
                {"op": "add", "kind": "constraint", "value": "当前话题短句",
                 "evidence": "当前话题短句"},
            ]),
            user_text="全对话不用感叹号，当前话题短句",
        ).snapshot
        # 另一个话题的旧事实（reset_topic 前的归档项）
        old = snapshot.facts[1].model_copy(
            update={"fact_id": "old", "scope_id": "topic:old", "value": "旧话题要求"}
        )
        snapshot.facts.append(old)
        return snapshot

    def test_only_session_and_current_topic(self) -> None:
        snapshot = self._snapshot()
        values = [fact.value for fact in rules.applicable_facts(
            snapshot, topic_id=TOPIC
        )]
        assert values == ["全对话不用感叹号", "当前话题短句"]

    def test_artifact_requires_verified_object(self) -> None:
        snapshot = self._snapshot()
        artifact = rules.ContextFact(  # type: ignore[attr-defined]
            fact_id="af", kind="constraint", value="封面别用红色",
            scope="artifact", scope_id="draft:r1:1", status="active",
            evidence="封面别用红色", source_ref={"message_id": "m-1"},
            created_version=1,
        )
        snapshot.facts.append(artifact)
        assert [f.value for f in rules.applicable_facts(
            snapshot, topic_id=TOPIC, object_keys=["draft:other:1"]
        )] == ["全对话不用感叹号", "当前话题短句"]
        assert "封面别用红色" in [
            f.value for f in rules.applicable_facts(
                snapshot, topic_id=TOPIC, object_keys=["draft:r1:1"]
            )
        ]

    def test_retracted_facts_excluded(self) -> None:
        snapshot = self._snapshot()
        snapshot.facts[0].status = "retracted"
        values = [fact.value for fact in rules.applicable_facts(
            snapshot, topic_id=TOPIC
        )]
        assert "全对话不用感叹号" not in values


class _TextProvider(llm.BaseProvider):
    name = "text-only"

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        return LLMResult(text="好的", provider=self.name, model="text-1",
                         prompt_tokens=1, completion_tokens=1, cost_cny=0.001,
                         usage_status="actual")

    async def complete_with_tools(self, call: llm.LLMCall,
                                  on_delta: Any = None) -> LLMResult:
        return await self.complete(call)


class TestA11PromptVersionMerge:
    """A11：成功轮次的 prompt_versions 必须包含抽取记录，不能只有 supervisor。"""

    @pytest.mark.asyncio
    async def test_success_path_merges_ledger_versions(self) -> None:
        from xhs_agent.workflows.chat import run_turn

        class _Chat:
            max_tool_rounds = 1
            history_max_messages = 10
            history_token_budget = 10_000

        class _Cfg:
            chat = _Chat()

        ledger: dict[str, Any] = {
            "records": [{"task": "context_extract", "prompt_tokens": 100,
                         "completion_tokens": 20, "cost_cny": 0.01,
                         "usage_status": "actual"}],
            "calls": {}, "tool_calls": [], "components": {},
            "versions": {"context_extract": 1}, "run_id": "",
        }
        turn = await run_turn(
            _Cfg(), provider=_TextProvider(), history=[], user_text="请开始分析夜跑",
            run_tool=None, ledger=ledger, stream=False,
        )
        assert turn.prompt_versions.get("context_extract") == 1
        assert "chat_supervisor" in turn.prompt_versions
        # 抽取的已知消费仍计入合计，且只计一次（合并版本不双计 tokens / 费用）
        assert (turn.prompt_tokens, turn.completion_tokens) == (101, 21)
        assert turn.cost_cny == pytest.approx(0.011)

"""上下文事实的校验与状态迁移（S7.24）：纯函数口径。

覆盖方案 §4.2 / §4.3 的六类行为：证据匹配、作用域解析、显式撤回 / 替换、歧义与冲突转提案、
用户动作（确认 / 拒绝 / 撤回 / 替换 / 切换话题）与会话级限制的存活。
"""

from __future__ import annotations

import pytest

from xhs_agent.core import context_rules as rules
from xhs_agent.schemas import ContextDelta, ContextSnapshot, SchemaError

TOPIC = "topic:t1"
REFS = [{"run_id": "r1", "position": 1, "object_key": "draft:r1:1"}]


def add(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "op": "add", "kind": "constraint", "value": "控制在 300 字以内",
        "evidence": "300 字以内",
    }
    base.update(kwargs)
    return base


def delta(operations: list[dict[str, object]] | None = None,
          proposals: list[dict[str, object]] | None = None) -> ContextDelta:
    return ContextDelta.from_dict(
        {"operations": operations or [], "proposals": proposals or []}
    )


def apply(snapshot: ContextSnapshot, data: ContextDelta, *, user_text: str,
          version: int = 1, refs: list[dict[str, object]] | None = None) -> rules.DeltaResult:
    return rules.apply_delta(
        snapshot, data, user_text=user_text, message_id="m-1",
        version=version, topic_id=snapshot.topic_id or TOPIC, refs=refs or [],
    )


class TestEvidence:
    def test_ignores_whitespace_differences(self) -> None:
        assert rules.evidence_in("控制在\n300 字以内", "300 字以内")

    def test_too_short_evidence_is_rejected(self) -> None:
        assert rules.evidence_in("改成口语一点", "改") is False

    def test_transcription_is_not_evidence(self) -> None:
        assert rules.evidence_in("改成口语一点", "用户希望更口语") is False


class TestApplyDelta:
    def test_add_with_evidence_activates(self) -> None:
        result = apply(ContextSnapshot(topic_id=TOPIC),
                       delta([add()]), user_text="文案控制在 300 字以内")
        assert result.decisions[0]["decision"] == "applied"
        assert result.snapshot.facts[0].scope == "topic"
        assert result.snapshot.facts[0].scope_id == TOPIC

    def test_add_without_evidence_is_rejected(self) -> None:
        result = apply(ContextSnapshot(topic_id=TOPIC),
                       delta([add(evidence="用户想短一点")]), user_text="文案控制在 300 字以内")
        assert result.decisions[0]["reason"] == "evidence_mismatch"
        assert result.snapshot.facts == []

    def test_duplicate_is_rejected(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC), delta([add()]),
                      user_text="文案控制在 300 字以内")
        second = apply(first.snapshot, delta([add()]),
                       user_text="文案控制在 300 字以内", version=2)
        assert second.decisions[0]["reason"] == "duplicate"
        assert len(second.snapshot.facts) == 1

    def test_retract_marks_retracted_and_archives(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC), delta([add()]),
                      user_text="文案控制在 300 字以内")
        fact_id = first.snapshot.facts[0].fact_id
        second = apply(first.snapshot,
                       delta([{"op": "retract", "target_fact_id": fact_id,
                               "evidence": "不用限字数了"}]),
                       user_text="不用限字数了", version=2)
        assert second.decisions[0]["decision"] == "retracted"
        assert second.snapshot.facts[0].status == "retracted"
        assert [item.fact_id for item in second.snapshot.archived] == [fact_id]

    def test_replace_supersedes_old_fact(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC), delta([add()]),
                      user_text="文案控制在 300 字以内")
        old_id = first.snapshot.facts[0].fact_id
        second = apply(first.snapshot,
                       delta([{"op": "replace", "kind": "constraint",
                               "value": "控制在 100 字以内",
                               "target_fact_id": old_id,
                               "evidence": "100 字以内"}]),
                       user_text="改成 100 字以内", version=2)
        statuses = {fact.fact_id: fact.status for fact in second.snapshot.facts}
        new_fact = next(f for f in second.snapshot.facts if f.status == "active")
        assert statuses[old_id] == "superseded"
        assert new_fact.replaces == old_id

    def test_retract_missing_target_is_rejected(self) -> None:
        result = apply(ContextSnapshot(topic_id=TOPIC),
                       delta([{"op": "retract", "target_fact_id": "nope",
                               "evidence": "不用了"}]),
                       user_text="不用了")
        assert result.decisions[0]["reason"] == "target_missing"

    def test_artifact_scope_requires_verified_ref(self) -> None:
        result = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([add(scope="artifact", scope_id="draft:r9:1")]),
            user_text="文案控制在 300 字以内",
        )
        assert result.decisions[0]["decision"] == "pending_confirmation"
        assert result.decisions[0]["reason"] == "scope_unresolved"
        assert result.snapshot.facts == []

    def test_artifact_scope_with_ref_activates(self) -> None:
        result = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([add(scope="artifact", scope_id="draft:r1:1")]),
            user_text="文案控制在 300 字以内", refs=REFS,
        )
        assert result.decisions[0]["decision"] == "applied"
        assert result.snapshot.facts[0].scope == "artifact"

    def test_numeric_conflict_becomes_proposal(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC), delta([add()]),
                      user_text="文案控制在 300 字以内")
        second = apply(first.snapshot,
                       delta([add(value="控制在 100 字以内", evidence="100 字以内")]),
                       user_text="改成 100 字以内", version=2)
        assert second.decisions[0]["decision"] == "pending_confirmation"
        assert second.decisions[0]["reason"] == "conflict"
        assert second.snapshot.facts[0].value == "控制在 300 字以内"  # 旧限制没被覆盖
        proposal = second.snapshot.proposals[0]
        assert proposal.conflicts_with == [first.snapshot.facts[0].fact_id]

    def test_inferred_preference_becomes_proposal(self) -> None:
        result = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta(proposals=[{"kind": "preference", "value": "更口语", "reason": "inferred"}]),
            user_text="随便改改",
        )
        assert result.decisions[0]["decision"] == "pending_confirmation"
        assert result.snapshot.proposals[0].reason == "inferred"
        assert result.pending == 1

    def test_proposal_evidence_is_also_validated(self) -> None:
        result = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta(proposals=[{"kind": "goal", "value": "蹭热点", "evidence": "用户想蹭",
                              "reason": "conflict"}]),
            user_text="换一个热点",
        )
        assert result.decisions[0]["reason"] == "evidence_mismatch"

    def test_multiple_preferences_coexist(self) -> None:
        first = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([add(kind="preference", value="口语一点", evidence="口语一点")]),
            user_text="口语一点",
        )
        second = apply(
            first.snapshot,
            delta([add(kind="preference", value="多用短句", evidence="多用短句")]),
            user_text="多用短句", version=2,
        )
        assert second.decisions[0]["decision"] == "applied"
        assert len(second.snapshot.facts) == 2


class TestActions:
    def _with_proposal(self) -> tuple[ContextSnapshot, str]:
        result = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta(proposals=[{"kind": "preference", "value": "更口语", "reason": "inferred"}]),
            user_text="随便改改",
        )
        return result.snapshot, result.snapshot.proposals[0].proposal_id

    def test_confirm_turns_proposal_into_fact(self) -> None:
        snapshot, proposal_id = self._with_proposal()
        acted = rules.apply_action(snapshot, action="confirm", target_id=proposal_id,
                                   topic_id=TOPIC, version=2)
        assert acted.decision == "applied"
        assert acted.snapshot.proposals == []
        fact = acted.snapshot.facts[0]
        assert fact.value == "更口语"
        assert fact.source_ref.get("action_id", "").startswith("confirm:")

    def test_reject_drops_proposal(self) -> None:
        snapshot, proposal_id = self._with_proposal()
        acted = rules.apply_action(snapshot, action="reject", target_id=proposal_id,
                                   topic_id=TOPIC, version=2)
        assert acted.snapshot.proposals == []
        assert acted.snapshot.facts == []

    def test_confirm_unknown_proposal_is_noop(self) -> None:
        snapshot, _ = self._with_proposal()
        acted = rules.apply_action(snapshot, action="confirm", target_id="nope",
                                   topic_id=TOPIC, version=2)
        assert acted.decision == "noop"

    def test_replace_requires_value(self) -> None:
        first = apply(ContextSnapshot(topic_id=TOPIC), delta([add()]),
                      user_text="文案控制在 300 字以内")
        acted = rules.apply_action(
            first.snapshot, action="replace",
            target_id=first.snapshot.facts[0].fact_id,
            value="", topic_id=TOPIC, version=2,
        )
        assert acted.decision == "noop" and acted.reason == "value_required"

    def test_reset_topic_archives_topic_facts_but_keeps_session_ones(self) -> None:
        session_fact = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([add(scope="session", value="全对话都用中文", evidence="全对话都用中文")]),
            user_text="全对话都用中文",
        )
        topic_fact = apply(
            session_fact.snapshot, delta([add()]),
            user_text="文案控制在 300 字以内", version=2,
        )
        reset = rules.apply_action(topic_fact.snapshot, action="reset_topic",
                                   topic_id=TOPIC, version=3)
        assert reset.snapshot.topic_id != TOPIC
        active = [fact.value for fact in reset.snapshot.active()]
        assert active == ["全对话都用中文"]
        assert len(reset.snapshot.archived) == 1


class TestBlocking:
    def test_only_conflict_and_ambiguity_block(self) -> None:
        snapshot = ContextSnapshot(topic_id=TOPIC)
        snapshot = apply(
            snapshot,
            delta(proposals=[
                {"kind": "preference", "value": "更口语", "reason": "inferred"},
                {"kind": "goal", "value": "换一个", "reason": "ambiguous_reference"},
            ]),
            user_text="随便改改",
        ).snapshot
        blocking = rules.blocking_proposals(snapshot)
        assert [item.reason for item in blocking] == ["ambiguous_reference"]

    def test_created_version_filter(self) -> None:
        snapshot = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta(proposals=[{"kind": "goal", "value": "换一个",
                              "reason": "ambiguous_reference"}]),
            user_text="换一个", version=2,
        ).snapshot
        assert rules.blocking_proposals(snapshot, created_version=2)
        assert rules.blocking_proposals(snapshot, created_version=3) == []


class TestRender:
    def test_render_state_contains_active_and_pending(self) -> None:
        snapshot = apply(
            ContextSnapshot(topic_id=TOPIC),
            delta([add()], proposals=[{"kind": "preference", "value": "更口语",
                                       "reason": "inferred"}]),
            user_text="文案控制在 300 字以内",
        ).snapshot
        rendered = rules.render_state(snapshot)
        assert "控制在 300 字以内" in rendered
        assert "pending_confirmation" in rendered

    def test_snapshot_parsing_failure_degrades_to_partial(self) -> None:
        snapshot = ContextSnapshot.from_dict({"facts": [{"kind": "nope"}]})
        assert snapshot.recovery_status == "partial"
        assert snapshot.recovery_reason


class TestSchemaGuards:
    def test_fact_requires_evidence(self) -> None:
        from xhs_agent.schemas import ContextFact
        with pytest.raises(SchemaError):
            ContextFact.from_dict(
                {"fact_id": "f", "kind": "goal", "value": "v",
                 "source_ref": {"message_id": "m"}}
            )

    def test_fact_rejects_unknown_kind(self) -> None:
        from xhs_agent.schemas import ContextFact
        with pytest.raises(SchemaError):
            ContextFact.from_dict(
                {"fact_id": "f", "kind": "unknown", "value": "v",
                 "evidence": "v", "source_ref": {"message_id": "m"}}
            )

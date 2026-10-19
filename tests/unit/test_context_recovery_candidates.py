"""恢复累计候选的跨页撤回 / 替换，不允许未经确认激活事实。"""
from xhs_agent.core import context_rules as rules
from xhs_agent.schemas import ContextDelta, ContextSnapshot


def apply(snapshot, operations, message, body, version):
    return rules.recover_candidates(snapshot, ContextDelta.from_dict({"operations": operations}),
        page_bodies={message: body}, recovery_id="r1", version=version, topic_id="t1")


def initial():
    snapshot, _, errors = apply(ContextSnapshot(topic_id="t1"), [
        {"op": "add", "kind": "constraint", "value": "300字以内", "evidence": "300字以内"}
    ], "m1", "控制在300字以内", 1)
    assert not errors and not snapshot.facts
    return snapshot


def test_later_page_retracts_pending_candidate():
    snapshot = initial()
    updated, decisions, errors = apply(snapshot, [{"op": "retract",
        "target_fact_id": snapshot.proposals[0].proposal_id, "evidence": "撤回字数限制"}],
        "m2", "撤回字数限制", 2)
    assert not errors and not updated.proposals and not updated.facts
    assert decisions[0]["decision"] == "retracted"
    assert len(snapshot.proposals) == 1


def test_later_page_replace_keeps_source_lineage():
    snapshot = initial()
    updated, _, errors = apply(snapshot, [{"op": "replace",
        "target_fact_id": snapshot.proposals[0].proposal_id, "value": "100字以内",
        "evidence": "改成100字以内"}], "m2", "改成100字以内", 2)
    assert not errors and not updated.facts
    assert [p.value for p in updated.proposals] == ["100字以内"]
    assert updated.proposals[0].source_message_ids == ["m1", "m2"]
    assert updated.proposals[0].operation == "add"


def test_invalid_target_rejects_entire_page():
    snapshot = initial()
    updated, decisions, errors = apply(snapshot, [
        {"op": "add", "kind": "goal", "value": "写露营笔记", "evidence": "写露营笔记"},
        {"op": "retract", "target_fact_id": "missing", "evidence": "撤回限制"},
    ], "m2", "写露营笔记，撤回限制", 2)
    assert errors == ["target_invalid"] and not decisions
    assert updated.to_dict() == snapshot.to_dict()


def test_retracting_active_fact_requires_confirmation():
    snapshot = rules.apply_delta(ContextSnapshot(topic_id="t1"), ContextDelta.from_dict({
        "operations": [{"op": "add", "kind": "constraint", "value": "300字以内", "evidence": "300字以内"}]
    }), user_text="300字以内", message_id="m1", version=1, topic_id="t1").snapshot
    updated, _, errors = apply(snapshot, [{"op": "retract",
        "target_fact_id": snapshot.facts[0].fact_id, "evidence": "撤回字数限制"}],
        "m2", "撤回字数限制", 2)
    assert not errors and updated.facts[0].status == "active"
    confirmed = rules.apply_action(updated, action="confirm", target_id=updated.proposals[0].proposal_id,
                                    topic_id="t1", version=3)
    assert confirmed.snapshot.facts[0].status == "retracted"
    assert not confirmed.snapshot.proposals and len(confirmed.snapshot.facts) == 1

"""上下文事实的校验与状态迁移（S7.24，纯函数）。

用途：把「模型给的增量」变成「服务端愿意接受的状态变更」——证据、作用域、目标对象、冲突与
去重都在这里判定，再把结果交给 `services/context_state.py` 做 CAS 与审计。
输入：现有 `ContextSnapshot`、`ContextDelta`（或用户动作）、本轮原文、消息 ID、新版本号、引用。
输出：新的快照 + 逐条裁决（`applied` / `pending_confirmation` / `rejected` / `superseded` /
      `retracted`）。

三条硬规则（对应方案 §4.2 / §4.3）：

1. **证据是必要条件**：`evidence` 必须能在本轮用户原文里逐字找到（忽略空白差异），匹配不上
   整条拒绝；模型的置信度字段不参与放行；
2. **推断 / 歧义 / 冲突不自动生效**：转成 `proposal`（待确认），确认才变事实；
3. **替换必须显式**：`replace` 要指到现有 `fact_id`，旧项转 `superseded` 并保留历史；
   `add` 与现有项数值冲突时不覆盖旧项，转提案。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..schemas import (
    MAX_FACTS_PER_SCOPE,
    ContextDelta,
    ContextFact,
    ContextOperation,
    ContextProposal,
    ContextSnapshot,
    SchemaError,
)

#: 允许"同类型多条并存"的类型；其余类型同作用域出现矛盾数值时转提案
MULTI_VALUE_KINDS = ("preference", "audience")
#: 数值型限制的单位（用于确定性的矛盾检测：同单位、不同数字 = 冲突）
_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(字|秒|分钟|个|条|张|篇|句|行)")
#: 证据片段的最短长度：一两个字很容易在长文里偶然命中
MIN_EVIDENCE_CHARS = 2
#: 会**阻断当前任务**的提案原因：冲突与歧义必须先确认，推断类只提示、不阻断
#: （方案 §4.2 第 7 步：存在阻断项时返回确认提示，不能一边询问一边继续改稿或分析）
BLOCKING_REASONS = ("conflict", "ambiguous_reference", "scope_unresolved")


def _norm(text: Any) -> str:
    """比较用的归一：去掉所有空白（换行 / 全角空格），保留标点与大小写。"""
    return "".join(str(text or "").split())


def evidence_in(user_text: str, evidence: str) -> bool:
    """证据是否是原文的真实片段（忽略空白差异，最短 2 字）。"""
    needle = _norm(evidence)
    if len(needle) < MIN_EVIDENCE_CHARS:
        return False
    return needle in _norm(user_text)


def new_fact_id() -> str:
    return f"fact_{uuid.uuid4().hex[:12]}"


def new_proposal_id() -> str:
    return f"prop_{uuid.uuid4().hex[:12]}"


def new_topic_id() -> str:
    return f"topic:{uuid.uuid4().hex[:12]}"


def _numeric_limits(value: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw, unit in _UNIT_RE.findall(str(value or "")):
        try:
            out[unit] = float(raw)
        except ValueError:  # pragma: no cover - 正则已保证是数字
            continue
    return out


def numeric_conflict(new_value: str, old_value: str) -> bool:
    """同单位、不同数字 → 视为数值冲突（例如「300 字以内」与「100 字以内」）。"""
    new_limits = _numeric_limits(new_value)
    old_limits = _numeric_limits(old_value)
    for unit, number in new_limits.items():
        if unit in old_limits and old_limits[unit] != number:
            return True
    return False


def _same_value(left: str, right: str) -> bool:
    return _norm(left) == _norm(right)


def _find_fact(snapshot: ContextSnapshot, fact_id: str) -> ContextFact | None:
    for fact in snapshot.facts:
        if fact.fact_id == fact_id:
            return fact
    return None


def _resolve_scope(
    *, scope: str, scope_id: str, topic_id: str, refs: list[dict[str, Any]]
) -> tuple[str, str, str]:
    """返回（最终作用域, 作用域对象, 拒绝原因）。

    `artifact` 必须在**服务端已验证的引用**里出现，否则不得激活（方案 §4.1）。
    """
    chosen = scope if scope in ("session", "topic", "artifact") else "topic"
    target = str(scope_id or "").strip()
    if chosen == "session":
        return "session", "", ""
    if chosen == "topic":
        return "topic", target or topic_id, ""
    keys = {str(item.get("object_key") or "") for item in refs}
    if not target or target not in keys:
        return "topic", topic_id, "scope_unresolved"
    return "artifact", target, ""


def _append_proposal(
    proposals: list[ContextProposal],
    *,
    kind: str,
    value: str,
    scope: str,
    scope_id: str,
    reason: str,
    evidence: str,
    message_id: str,
    version: int,
    conflicts_with: list[str] | None = None,
) -> ContextProposal | None:
    for item in proposals:
        if item.kind == kind and _same_value(item.value, value) and item.scope == scope:
            return None  # 已有等价提案，不重复占位
    try:
        proposal = ContextProposal(
            proposal_id=new_proposal_id(),
            kind=kind,
            value=value,
            scope=scope,
            scope_id=scope_id,
            reason=reason,
            evidence=evidence,
            source_ref={"message_id": message_id},
            conflicts_with=list(conflicts_with or []),
            created_version=version,
        )
    except SchemaError:
        return None
    proposals.append(proposal)
    return proposal


def _cap_facts(snapshot: ContextSnapshot, *, version: int) -> list[str]:
    """作用域内事实过多时归档最旧的**非 session** 项；返回被归档的 fact_id。

    session 级限制不自动丢——它们只在用户撤回 / 替换时失效（方案 §4.1）。
    """
    by_scope: dict[str, list[ContextFact]] = {}
    for fact in snapshot.facts:
        if fact.scope != "session":
            by_scope.setdefault(fact.scope_id or fact.scope, []).append(fact)
    archived: list[str] = []
    for _key, items in by_scope.items():
        active = [item for item in items if item.status == "active"]
        if len(active) <= MAX_FACTS_PER_SCOPE:
            continue
        overflow = len(active) - MAX_FACTS_PER_SCOPE
        for fact in sorted(active, key=lambda item: (item.created_version, item.fact_id))[:overflow]:
            index = next(
                (i for i, item in enumerate(snapshot.facts) if item is fact), -1
            )
            if index < 0:  # pragma: no cover - 列表由上面同一批对象构成
                continue
            archived_fact = fact.model_copy(update={"status": "superseded"})
            snapshot.facts[index] = archived_fact
            snapshot.archived.append(archived_fact)
            archived.append(fact.fact_id)
    _ = version
    return archived


@dataclass
class DeltaResult:
    """一次增量的应用结果。"""

    snapshot: ContextSnapshot
    decisions: list[dict[str, Any]] = field(default_factory=list)
    applied: int = 0
    pending: int = 0
    rejected: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.pending)


@dataclass
class ActionResult:
    """一次用户动作的应用结果。"""

    snapshot: ContextSnapshot
    decision: str = "applied"       # applied / noop
    reason: str = ""
    event_type: str = "confirm"


def _decision(
    fact_id: str, kind: str, decision: str, reason: str = ""
) -> dict[str, Any]:
    return {"fact_id": fact_id, "kind": kind, "decision": decision, "reason": reason}


def apply_delta(
    snapshot: ContextSnapshot,
    delta: ContextDelta,
    *,
    user_text: str,
    message_id: str,
    version: int,
    topic_id: str,
    refs: list[dict[str, Any]] | None = None,
) -> DeltaResult:
    """把抽取增量应用到快照副本上（不写库；调用方负责 CAS / 审计）。

    `version` 是**本次变更后的版本号**（调用方按当前版本 +1 分配）；事实与提案的
    `created_version` 都记它，便于审计回放。
    """
    working = snapshot.model_copy(deep=True)
    result = DeltaResult(snapshot=working)
    refs = list(refs or [])

    for op in delta.operations:
        decision = _apply_operation(
            working, op, user_text=user_text, message_id=message_id,
            version=version, topic_id=topic_id, refs=refs,
        )
        result.decisions.append(decision)
        if decision["decision"] == "applied":
            result.applied += 1
        elif decision["decision"] == "pending_confirmation":
            result.pending += 1
        else:
            result.rejected += 1

    for raw in delta.proposals:
        mark = _apply_proposal(
            working, raw, user_text=user_text, message_id=message_id,
            version=version, topic_id=topic_id, refs=refs,
        )
        result.decisions.append(mark)
        if mark["decision"] == "pending_confirmation":
            result.pending += 1
        else:
            result.rejected += 1

    _cap_facts(working, version=version)
    return result


def _apply_operation(
    working: ContextSnapshot,
    op: ContextOperation,
    *,
    user_text: str,
    message_id: str,
    version: int,
    topic_id: str,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    if not evidence_in(user_text, op.evidence):
        return _decision(op.target_fact_id or "", op.kind, "rejected", "evidence_mismatch")

    if op.op in ("retract", "replace"):
        target = _find_fact(working, op.target_fact_id)
        if target is None:
            return _decision(op.target_fact_id, op.kind or target_kind(target),
                             "rejected", "target_missing")
        if target.status not in ("active", "pending_confirmation"):
            return _decision(target.fact_id, target.kind, "rejected", "target_inactive")
        if op.op == "retract":
            _mark_status(working, target, "retracted", version)
            return _decision(target.fact_id, target.kind, "retracted", "explicit_retract")
        return _replace_fact(
            working, target, op, message_id=message_id, version=version, topic_id=topic_id,
        )

    # ---- add ----
    scope, scope_id, reject = _resolve_scope(
        scope=op.scope, scope_id=op.scope_id, topic_id=topic_id, refs=refs,
    )
    if reject:
        _append_proposal(
            working.proposals, kind=op.kind, value=op.value, scope="topic",
            scope_id=topic_id, reason=reject, evidence=op.evidence,
            message_id=message_id, version=version,
        )
        return _decision("", op.kind, "pending_confirmation", reject)

    for fact in working.facts:
        if (
            fact.status == "active"
            and fact.kind == op.kind
            and fact.scope == scope
            and (fact.scope_id or "") == (scope_id or "")
            and _same_value(fact.value, op.value)
        ):
            return _decision(fact.fact_id, fact.kind, "rejected", "duplicate")

    conflicts = [
        fact.fact_id
        for fact in working.facts
        if fact.status == "active"
        and fact.kind == op.kind
        and fact.scope == scope
        and (fact.scope_id or "") == (scope_id or "")
        and fact.kind not in MULTI_VALUE_KINDS
        and numeric_conflict(op.value, fact.value)
    ]
    if conflicts:
        proposal = _append_proposal(
            working.proposals, kind=op.kind, value=op.value, scope=scope,
            scope_id=scope_id, reason="conflict", evidence=op.evidence,
            message_id=message_id, version=version, conflicts_with=conflicts,
        )
        return _decision(
            proposal.proposal_id if proposal else "", op.kind,
            "pending_confirmation", "conflict",
        )

    fact = ContextFact(
        fact_id=new_fact_id(),
        kind=op.kind,
        value=op.value,
        scope=scope,
        scope_id=scope_id,
        status="active",
        source_ref={"message_id": message_id},
        evidence=op.evidence,
        created_version=version,
    )
    working.facts.append(fact)
    return _decision(fact.fact_id, fact.kind, "applied", op.reason or "explicit_add")


def target_kind(fact: ContextFact | None) -> str:
    return fact.kind if fact is not None else ""


def _replace_fact(
    working: ContextSnapshot,
    target: ContextFact,
    op: ContextOperation,
    *,
    message_id: str,
    version: int,
    topic_id: str,
) -> dict[str, Any]:
    scope, scope_id, reject = _resolve_scope(
        scope=op.scope if op.scope else target.scope,
        scope_id=op.scope_id or target.scope_id,
        topic_id=topic_id,
        refs=working.object_refs,
    )
    if reject:
        return _decision(target.fact_id, target.kind, "rejected", reject)
    _mark_status(working, target, "superseded", version)
    replacement = ContextFact(
        fact_id=new_fact_id(),
        kind=target.kind,
        value=op.value,
        scope=scope,
        scope_id=scope_id,
        status="active",
        source_ref={"message_id": message_id},
        evidence=op.evidence,
        created_version=version,
        replaces=target.fact_id,
    )
    working.facts.append(replacement)
    return _decision(replacement.fact_id, replacement.kind, "applied", "explicit_replace")


def _apply_proposal(
    working: ContextSnapshot,
    raw: dict[str, Any],
    *,
    user_text: str,
    message_id: str,
    version: int,
    topic_id: str,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    kind = str(raw.get("kind") or "").strip()
    value = str(raw.get("value") or "").strip()
    reason = str(raw.get("reason") or "inferred").strip() or "inferred"
    evidence = str(raw.get("evidence") or "").strip()
    if evidence and not evidence_in(user_text, evidence):
        return _decision("", kind, "rejected", "evidence_mismatch")
    scope, scope_id, reject = _resolve_scope(
        scope=str(raw.get("scope") or "topic"), scope_id=str(raw.get("scope_id") or ""),
        topic_id=topic_id, refs=refs,
    )
    if reject:
        reason = reject
    conflicts = [str(item) for item in (raw.get("conflicts_with") or []) if str(item)]
    proposal = _append_proposal(
        working.proposals, kind=kind, value=value, scope=scope, scope_id=scope_id,
        reason=reason, evidence=evidence, message_id=message_id, version=version,
        conflicts_with=conflicts,
    )
    if proposal is None:
        return _decision("", kind, "rejected", "invalid_or_duplicate")
    return _decision(proposal.proposal_id, kind, "pending_confirmation", reason)


def _mark_status(
    working: ContextSnapshot, fact: ContextFact, status: str, version: int
) -> None:
    """就地更新状态；`superseded` / `retracted` 同时进归档（保留历史，不删除）。"""
    updated = fact.model_copy(update={"status": status, "created_version": fact.created_version})
    index = next(
        (i for i, item in enumerate(working.facts) if item.fact_id == fact.fact_id), -1
    )
    if index >= 0:
        working.facts[index] = updated
    if status in ("superseded", "retracted"):
        working.archived.append(updated)
    _ = version


# ---------------------------------------------------------------------------
# 用户动作
# ---------------------------------------------------------------------------


def apply_action(
    snapshot: ContextSnapshot,
    *,
    action: str,
    target_id: str = "",
    value: str = "",
    topic_id: str,
    version: int,
) -> ActionResult:
    """确认 / 拒绝 / 撤回 / 替换 / 切换话题（用户动作，不需要证据——动作本身就是来源）。

    返回的 `decision` 为 `noop` 时调用方**不推进版本**（避免空转的审计行）。
    """
    working = snapshot.model_copy(deep=True)
    target = str(target_id or "").strip()

    if action == "reset_topic":
        for index, fact in enumerate(working.facts):
            if fact.scope == "topic" and fact.status in ("active", "pending_confirmation"):
                archived = fact.model_copy(update={"status": "superseded"})
                working.facts[index] = archived
                working.archived.append(archived)
        kept = [item for item in working.proposals if item.scope != "topic"]
        working.proposals = kept
        working.topic_id = new_topic_id()
        return ActionResult(snapshot=working, event_type="reset_topic")

    if action in ("confirm", "reject") and target:
        proposal = next(
            (item for item in working.proposals if item.proposal_id == target), None
        )
        if proposal is None:
            return ActionResult(snapshot=working, decision="noop", reason="proposal_missing")
        working.proposals = [item for item in working.proposals if item.proposal_id != target]
        if action == "reject":
            return ActionResult(snapshot=working, event_type="reject")
        fact = ContextFact(
            fact_id=new_fact_id(),
            kind=proposal.kind,
            value=proposal.value,
            scope=proposal.scope,
            scope_id=proposal.scope_id,
            status="active",
            source_ref={
                "action_id": f"confirm:{target}",
                **(
                    {"message_id": proposal.source_ref.get("message_id")}
                    if proposal.source_ref.get("message_id")
                    else {}
                ),
            },
            evidence=proposal.evidence or "用户确认",
            created_version=version,
        )
        working.facts.append(fact)
        return ActionResult(snapshot=working, event_type="confirm")

    if action == "retract" and target:
        target_fact = _find_fact(working, target)
        if target_fact is None or target_fact.status != "active":
            return ActionResult(snapshot=working, decision="noop", reason="fact_missing")
        _mark_status(working, target_fact, "retracted", version)
        return ActionResult(snapshot=working, event_type="retract")

    if action == "replace" and target:
        target_fact = _find_fact(working, target)
        new_value = str(value or "").strip()
        if target_fact is None or target_fact.status != "active":
            return ActionResult(snapshot=working, decision="noop", reason="fact_missing")
        if not new_value:
            return ActionResult(snapshot=working, decision="noop", reason="value_required")
        _mark_status(working, target_fact, "superseded", version)
        working.facts.append(
            ContextFact(
                fact_id=new_fact_id(),
                kind=target_fact.kind,
                value=new_value,
                scope=target_fact.scope,
                scope_id=target_fact.scope_id,
                status="active",
                source_ref={"action_id": f"replace:{target}"},
                evidence="用户显式替换",
                created_version=version,
                replaces=target_fact.fact_id,
            )
        )
        return ActionResult(snapshot=working, event_type="replace")

    return ActionResult(snapshot=working, decision="noop", reason="invalid_action")


# ---------------------------------------------------------------------------
# 渲染给模型 / 接口
# ---------------------------------------------------------------------------


def render_state(snapshot: ContextSnapshot, *, limit: int = 20) -> str:
    """现有状态 → 抽取 prompt 的 `state` 占位符（只给生效 / 待确认项，最多 20 条）。"""
    import json

    items: list[dict[str, Any]] = []
    for fact in snapshot.active():
        items.append(
            {"fact_id": fact.fact_id, "kind": fact.kind, "value": fact.value,
             "scope": fact.scope, "scope_id": fact.scope_id, "status": fact.status}
        )
    for proposal in snapshot.pending():
        items.append(
            {"fact_id": proposal.proposal_id, "kind": proposal.kind, "value": proposal.value,
             "scope": proposal.scope, "scope_id": proposal.scope_id,
             "status": "pending_confirmation"}
        )
    return json.dumps(items[:limit], ensure_ascii=False)


def fact_view(fact: ContextFact) -> dict[str, Any]:
    return fact.to_dict()


def proposal_view(proposal: ContextProposal) -> dict[str, Any]:
    return proposal.to_dict()


def blocking_proposals(
    snapshot: ContextSnapshot, *, created_version: int | None = None
) -> list[ContextProposal]:
    """会阻断当前任务的提案（冲突 / 歧义 / 作用域未解析）。

    `created_version` 给了就只看这一版新增的——上一轮遗留的提案不重复阻断，
    但仍会在状态面板里显示（用户可随时处理）。
    """
    out = []
    for proposal in snapshot.proposals:
        if proposal.reason not in BLOCKING_REASONS:
            continue
        if created_version is not None and proposal.created_version != created_version:
            continue
        out.append(proposal)
    return out

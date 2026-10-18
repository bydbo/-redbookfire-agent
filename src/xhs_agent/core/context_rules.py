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
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..schemas import (
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


#: 恢复状态（v2 §3.5）；只有 `complete` 表示"已核对当前要求"，其余一律按未完成处理
RECOVERY_STATUSES = (
    "complete",
    "pending",
    "running",
    "awaiting_confirmation",
    "failed",
    "interrupted",
)


def new_recovery(
    *,
    status: str = "complete",
    reason: str = "new_session",
    baseline_created_at: str = "",
    baseline_message_id: str = "",
) -> dict[str, Any]:
    """构造一份完整的恢复结构（A01：新建会话必须持久化维护起点）。

    `baseline_*` 是**维护起点**：状态抽取只对起点之后的消息负责。新会话写入起点即
    `status=complete`，首轮不要求先恢复；旧数据没有覆盖证据时由调用方另标 `pending`。
    """
    return {
        "status": status if status in RECOVERY_STATUSES else "pending",
        "reason": str(reason or ""),
        "recovery_id": None,
        "baseline_created_at": str(baseline_created_at or ""),
        "baseline_message_id": str(baseline_message_id or ""),
        "snapshot_last_created_at": None,
        "snapshot_last_message_id": None,
        "cursor_created_at": None,
        "cursor_message_id": None,
        "scanned_user_messages": 0,
        "total_user_messages": 0,
        "pending_proposal_ids": [],
        "accounting_message_id": None,
        "gaps": [],
    }


def recovery_of(raw_state: dict[str, Any] | None) -> dict[str, Any]:
    """从 `chat_sessions.context_state` 读出恢复结构（A01：坏字段不无声丢弃后假定完整）。

    - 没有 `recovery` 键 → 返回空 dict（由 `load` 按历史覆盖证据判定）；
    - 有键但 `status` 非法 / 缺失 → 保持原字段并降级为 `pending`（`recovery_corrupt`）。
    """
    raw = (raw_state or {}).get("recovery")
    if not isinstance(raw, dict) or not raw:
        return {}
    status = str(raw.get("status") or "")
    if status not in RECOVERY_STATUSES:
        return {**raw, "status": "pending",
                "reason": str(raw.get("reason") or "recovery_corrupt")}
    return dict(raw)



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


def _load_ref(value: Any) -> dict[str, Any]:
    """对象引用的宽松解析：dict 直用；JSON 字符串解析失败返回空 dict（由上层报无操作）。"""
    import json

    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
    operation: str = "add",
    replaces_fact_ids: list[str] | None = None,
    target_object_ref: dict[str, Any] | None = None,
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
            operation=operation,
            replaces_fact_ids=list(replaces_fact_ids or []),
            target_object_ref=dict(target_object_ref or {}),
            source_message_ids=[message_id] if message_id else [],
        )
    except SchemaError:
        return None
    proposals.append(proposal)
    return proposal


@dataclass
class DeltaResult:
    """一次增量的应用结果。

    E7CTX-01：撤回 / 替换都是真实状态变更——`retracted` / `superseded` 参与变更判定，
    不再把"applied"当唯一依据。E7CTX-03：任何一条操作验证失败（证据 / 目标 / 作用域）
    时**整批不提交**（`invalid=True`），依赖工具不得启动；等价重复项是无操作，不算失败。
    """

    snapshot: ContextSnapshot
    decisions: list[dict[str, Any]] = field(default_factory=list)
    applied: int = 0
    pending: int = 0
    rejected: int = 0
    retracted: int = 0
    superseded: int = 0
    invalid: bool = False
    invalid_reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.pending or self.retracted or self.superseded)


@dataclass
class ActionResult:
    """一次用户动作的应用结果。

    `decision=conflict` 表示确认替换时旧项已变化（E7CTX-04）：服务层把它折成 409，
    不留下"旧项已撤销、新项未落库"的半状态。
    """

    snapshot: ContextSnapshot
    decision: str = "applied"       # applied / noop / conflict
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

    两段式（E7CTX-03）：先**整批校验**（证据 / 目标 / 作用域），任何一条硬性验证失败
    都不提交、依赖工具不启动；等价重复项是正常无操作。`version` 是本次变更后的版本号
    （调用方按当前版本 +1 分配），事实与提案的 `created_version` 都记它。
    """
    working = snapshot.model_copy(deep=True)
    result = DeltaResult(snapshot=working)
    refs = list(refs or [])

    # ---- 第一段：整批验证（不改动快照）----
    validation_failures: list[str] = []
    plan: list[tuple[Any, dict[str, Any]]] = []   # ("op", outcome) 或 ("proposal", outcome)
    for op in delta.operations:
        outcome = _validate_operation(working, op, user_text=user_text, topic_id=topic_id,
                                      refs=refs)
        if outcome["decision"] in ("applied", "retracted", "superseded", "pending_confirmation"):
            plan.append((op, outcome))
            result.decisions.append(outcome)
        elif outcome["decision"] == "noop":
            result.decisions.append(outcome)   # 等价重复：无操作，不算失败
        else:
            validation_failures.append(str(outcome.get("reason") or "invalid"))
            result.decisions.append(outcome)
    for raw in delta.proposals:
        outcome = _validate_proposal(raw, user_text=user_text, topic_id=topic_id, refs=refs)
        if outcome["decision"] == "pending_confirmation":
            plan.append(("proposal", outcome))
            result.decisions.append(outcome)
        elif outcome["decision"] == "noop":
            result.decisions.append(outcome)
        else:
            validation_failures.append(str(outcome.get("reason") or "invalid"))
            result.decisions.append(outcome)

    if validation_failures:
        # 整批不提交：快照保持原样，由调用方停止依赖本次增量的任务
        result.invalid = True
        result.invalid_reason = validation_failures[0]
        result.snapshot = snapshot.model_copy(deep=True)
        result.applied = result.pending = result.retracted = result.superseded = 0
        return result

    # ---- 第二段：按计划应用（此时快照才会变化）----
    for _item, outcome in plan:
        if _item == "proposal":
            mark = _commit_proposal(
                working, outcome, user_text=user_text, message_id=message_id,
                version=version, topic_id=topic_id, refs=refs,
            )
            result.decisions[result.decisions.index(outcome)] = mark
            if mark["decision"] == "pending_confirmation":
                result.pending += 1
            continue
        decision = outcome["decision"]
        if decision == "applied":
            fact = _commit_add(working, outcome, message_id=message_id, version=version,
                               topic_id=topic_id)
            result.applied += 1
            _ = fact
        elif decision == "retracted":
            _mark_status(working, _find_fact(working, outcome["fact_id"]) or working.facts[0],
                         "retracted", version)
            result.retracted += 1
        elif decision == "superseded":
            replaced = _commit_replace(working, outcome, message_id=message_id,
                                       version=version, topic_id=topic_id)
            result.superseded += 1
            _ = replaced
        elif decision == "pending_confirmation":
            body = outcome.get("proposal")
            if body is not None:
                mark = _commit_proposal(
                    working, outcome, user_text=user_text, message_id=message_id,
                    version=version, topic_id=topic_id, refs=refs,
                )
                result.decisions[result.decisions.index(outcome)] = mark
            result.pending += 1

    return result


def _validate_operation(
    working: ContextSnapshot,
    op: ContextOperation,
    *,
    user_text: str,
    topic_id: str,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """单条操作的**验证**（只读）：返回裁决，不改快照。

    `applied` / `retracted` / `superseded` / `pending_confirmation` 表示验证通过、可执行；
    `noop` 表示等价重复；其余（`rejected` + reason）是硬性验证失败，触发整批拒绝。
    """
    if not evidence_in(user_text, op.evidence):
        return _decision(op.target_fact_id or "", op.kind, "rejected", "evidence_mismatch")

    if op.op in ("retract", "replace"):
        target = _find_fact(working, op.target_fact_id)
        if target is None:
            return _decision(op.target_fact_id, op.kind or target_kind(target),
                             "rejected", "target_missing")
        if target.status not in ("active", "pending_confirmation"):
            # 重复撤回 / 替换已失效事实 = 无操作（E7CTX-01：不推进版本，也不算批失败）
            return _decision(target.fact_id, target.kind, "noop", "target_inactive")
        if op.op == "retract":
            return _decision(target.fact_id, target.kind, "retracted", "explicit_retract")
        return {
            **_decision(target.fact_id, target.kind, "superseded", "explicit_replace"),
            "value": op.value, "evidence": op.evidence,
        }

    # ---- add ----
    scope, scope_id, reject = _resolve_scope(
        scope=op.scope, scope_id=op.scope_id, topic_id=topic_id, refs=refs,
    )
    if reject:
        return {
            **_decision("", op.kind, "pending_confirmation", reject),
            "proposal": {"kind": op.kind, "value": op.value, "scope": "topic",
                         "scope_id": topic_id, "reason": reject, "evidence": op.evidence},
        }

    for fact in working.facts:
        if (
            fact.status == "active"
            and fact.kind == op.kind
            and fact.scope == scope
            and (fact.scope_id or "") == (scope_id or "")
            and _same_value(fact.value, op.value)
        ):
            return _decision(fact.fact_id, fact.kind, "noop", "duplicate")

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
        return {
            **_decision("", op.kind, "pending_confirmation", "conflict"),
            "proposal": {"kind": op.kind, "value": op.value, "scope": scope,
                         "scope_id": scope_id, "reason": "conflict",
                         "evidence": op.evidence, "conflicts_with": conflicts,
                         "operation": "replace", "replaces_fact_ids": conflicts},
        }

    return {
        **_decision("", op.kind, "applied", op.reason or "explicit_add"),
        "scope": scope, "scope_id": scope_id,
        "value": op.value, "evidence": op.evidence,
    }


def _validate_proposal(
    raw: dict[str, Any],
    *,
    user_text: str,
    topic_id: str,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """提案内容的验证（只读）：证据缺失允许（提案可以无证据），证据错误拒绝。"""
    kind = str(raw.get("kind") or "").strip()
    value = str(raw.get("value") or "").strip()
    reason = str(raw.get("reason") or "inferred").strip() or "inferred"
    evidence = str(raw.get("evidence") or "").strip()
    if evidence and not evidence_in(user_text, evidence):
        return _decision("", kind, "rejected", "evidence_mismatch")
    if not value:
        return _decision("", kind, "rejected", "value_required")
    scope, scope_id, reject = _resolve_scope(
        scope=str(raw.get("scope") or "topic"), scope_id=str(raw.get("scope_id") or ""),
        topic_id=topic_id, refs=refs,
    )
    if reject:
        reason = reject
    conflicts = [str(item) for item in (raw.get("conflicts_with") or []) if str(item)]
    operation = str(raw.get("operation") or ("replace" if conflicts else "add"))
    return {
        **_decision("", kind, "pending_confirmation", reason),
        "proposal": {
            "kind": kind, "value": value, "scope": scope, "scope_id": scope_id,
            "reason": reason, "evidence": evidence, "conflicts_with": conflicts,
            "operation": operation,
            "replaces_fact_ids": [str(x) for x in (raw.get("replaces_fact_ids") or conflicts)],
            "target_object_ref": dict(raw.get("target_object_ref") or {}),
        },
    }


def _commit_add(
    working: ContextSnapshot,
    outcome: dict[str, Any],
    *,
    message_id: str,
    version: int,
    topic_id: str,
) -> ContextFact:
    op_kind = str(outcome.get("kind") or "goal")
    fact = ContextFact(
        fact_id=new_fact_id(),
        kind=op_kind,
        value=str(outcome.get("value") or ""),
        scope=str(outcome.get("scope") or "topic"),
        scope_id=str(outcome.get("scope_id") or (topic_id if outcome.get("scope") == "topic" else "")),
        status="active",
        source_ref={"message_id": message_id},
        evidence=str(outcome.get("evidence") or ""),
        created_version=version,
    )
    working.facts.append(fact)
    return fact


def _commit_replace(
    working: ContextSnapshot,
    outcome: dict[str, Any],
    *,
    message_id: str,
    version: int,
    topic_id: str,
) -> ContextFact:
    target = _find_fact(working, str(outcome.get("fact_id") or ""))
    assert target is not None  # 验证段已确认存在
    scope, scope_id, reject = _resolve_scope(
        scope=target.scope, scope_id=target.scope_id, topic_id=topic_id,
        refs=working.object_refs,
    )
    if reject:  # pragma: no cover - 验证段已解析过作用域
        scope, scope_id = target.scope, target.scope_id
    _mark_status(working, target, "superseded", version)
    replacement = ContextFact(
        fact_id=new_fact_id(),
        kind=target.kind,
        value=str(outcome.get("value") or ""),
        scope=scope,
        scope_id=scope_id,
        status="active",
        source_ref={"message_id": message_id},
        evidence=str(outcome.get("evidence") or ""),
        created_version=version,
        replaces=target.fact_id,
    )
    working.facts.append(replacement)
    return replacement


def _commit_proposal(
    working: ContextSnapshot,
    outcome: dict[str, Any],
    *,
    user_text: str,
    message_id: str,
    version: int,
    topic_id: str,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    body = dict(outcome.get("proposal") or {})
    proposal = _append_proposal(
        working.proposals,
        kind=str(body.get("kind") or ""),
        value=str(body.get("value") or ""),
        scope=str(body.get("scope") or "topic"),
        scope_id=str(body.get("scope_id") or ""),
        reason=str(body.get("reason") or "inferred"),
        evidence=str(body.get("evidence") or ""),
        message_id=message_id,
        version=version,
        conflicts_with=[str(x) for x in (body.get("conflicts_with") or [])],
        operation=str(body.get("operation") or "add"),
        replaces_fact_ids=[str(x) for x in (body.get("replaces_fact_ids") or [])],
        target_object_ref=dict(body.get("target_object_ref") or {}),
    )
    if proposal is None:
        return _decision("", str(body.get("kind") or ""), "noop", "invalid_or_duplicate")
    return _decision(proposal.proposal_id, proposal.kind, "pending_confirmation",
                     proposal.reason)


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
        if action == "reject":
            working.proposals = [
                item for item in working.proposals if item.proposal_id != target
            ]
            return ActionResult(snapshot=working, event_type="reject")

        # ---- 确认替换（E7CTX-04）：原子校验旧项 → 全部置 superseded → 新事实记录替代集合 ----
        replace_ids = list(proposal.replaces_fact_ids) or list(proposal.conflicts_with)
        if proposal.operation == "replace" and replace_ids:
            targets: list[ContextFact] = []
            for fact_id in replace_ids:
                old = _find_fact(working, fact_id)
                if old is None or old.status != "active":
                    return ActionResult(
                        snapshot=snapshot.model_copy(deep=True),   # 原快照原样返回
                        decision="conflict",
                        reason="replace_target_changed",
                        event_type="confirm",
                    )
                targets.append(old)
            for old in targets:
                _mark_status(working, old, "superseded", version)
        if proposal.operation == "select_object":
            # 对象未解析的提案不能只靠确认生效：必须带完整对象引用（服务层再验证归属）
            ref = dict(proposal.target_object_ref or {})
            if not str(ref.get("object_key") or ""):
                return ActionResult(snapshot=working, decision="noop",
                                    reason="object_ref_required")
            working.object_refs = [
                item for item in working.object_refs
                if str(item.get("object_key") or "") != str(ref.get("object_key") or "")
            ]
            working.object_refs.append(ref)
        replaces = replace_ids if proposal.operation == "replace" else []
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
            replaces=replaces[0] if len(replaces) == 1 else None,
            replaces_fact_ids=replaces,
        )
        working.facts.append(fact)
        working.proposals = [
            item for item in working.proposals if item.proposal_id != target
        ]
        return ActionResult(snapshot=working, event_type="confirm")

    if action == "select_object":
        ref = value if isinstance(value, dict) else _load_ref(value)
        if not str(ref.get("object_key") or ""):
            return ActionResult(snapshot=working, decision="noop", reason="object_ref_required")
        working.object_refs = [
            item for item in working.object_refs
            if str(item.get("object_key") or "") != str(ref.get("object_key") or "")
        ]
        working.object_refs.append(ref)
        return ActionResult(snapshot=working, event_type="select_object")

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
# 适用事实筛选（A05）
# ---------------------------------------------------------------------------


def applicable_facts(
    snapshot: ContextSnapshot,
    *,
    topic_id: str | None,
    object_keys: Iterable[str] = (),
) -> list[ContextFact]:
    """本轮**真正适用**的生效事实（A05 唯一筛选口径）。

    - `session`：一直适用；
    - `topic`：只取当前话题（`scope_id == topic_id`）；
    - `artifact`：只取**服务端已验证的目标对象**（`object_keys`，来自本轮 refs / 选定对象）；
      未解析对象的约束永远不会以 artifact 作用域生效（抽取阶段已转提案）；
    - 失效（superseded / retracted）与待确认项一律排除。
    """
    keys = {str(item) for item in object_keys if str(item)}
    current_topic = str(topic_id or "")
    out: list[ContextFact] = []
    for fact in snapshot.facts:
        if fact.status != "active":
            continue
        applicable = (
            fact.scope == "session"
            or (fact.scope == "topic" and (fact.scope_id or "") == current_topic)
            or (fact.scope == "artifact" and (fact.scope_id or "") in keys)
        )
        if applicable:
            out.append(fact)
    return out


def constraint_view(facts: Iterable[ContextFact]) -> list[dict[str, Any]]:
    """适用事实 → 交给各生成消费者的**同一份**约束快照（含身份、值、作用域、来源）。

    只放可追溯的字段；`source_ref` 保留真实消息 / 动作来源，便于用户回看这一条来自哪句话。
    """
    return [
        {
            "fact_id": fact.fact_id,
            "kind": fact.kind,
            "value": fact.value,
            "scope": fact.scope,
            "scope_id": fact.scope_id,
            "source_ref": dict(fact.source_ref),
        }
        for fact in facts
    ]


def snapshot_object_keys(snapshot: ContextSnapshot) -> list[str]:
    """服务端已验证的对象键（本次会话已选定 / 已解析的对象）。"""
    return [str(item.get("object_key") or "") for item in snapshot.object_refs
            if str(item.get("object_key") or "")]


# ---------------------------------------------------------------------------
# 渲染给模型 / 接口
# ---------------------------------------------------------------------------


def render_state(snapshot: ContextSnapshot) -> str:
    """现有状态 → 抽取 prompt 的 `state` 占位符（E7CTX-02：全部生效 / 待确认项，不截断）。"""
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
    return json.dumps(items, ensure_ascii=False)


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

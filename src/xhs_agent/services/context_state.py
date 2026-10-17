"""会话上下文状态用例（S7.24）：抽取、验证、CAS、审计、动作与恢复。

用途：把「本轮用户原文 + 现有状态」变成「可被装配器使用的结构化状态」，并保证四件事：
证据可追溯、作用域正确、事务不半提交、冲突不覆盖。
输入：`AsyncSession`、`AppConfig`、用户消息 ID 与原文、模型 caller、引用清单。
输出：`StoredContext` / `ApplyResult` / 上下文视图（供 API 与装配器）。

口径（方案 §4.2 / §4.3 / §5.2）：

- **抽取在历史裁剪之前**执行（由轮次入口调用），输入是本轮原文而不是被裁过的历史；
- 模型给的 `fact_id` / `status` / `created_version` 一律不采信——由服务端生成；
- **证据 = 用户原文片段**：逐字匹配不到就拒绝整条，置信度不参与放行；
- 应用走 `state_version` CAS：条件更新 + 审计同事务，冲突不覆盖、不留半个状态；
- `shadow` 模式只记录候选判定，**不写**状态与审计；
- 观测（`context_state_decision`）在事务成功后才写，旁路失败不影响业务。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core import context_rules
from ..core.errors import BadRequestError, ConflictError, NotFoundError
from ..core.observations import record as record_observation
from ..db.models import ChatContextEvent, ChatMessage, ChatSession
from ..schemas import (
    CONTEXT_ACTIONS,
    ContextDelta,
    ContextSnapshot,
    SchemaError,
)
from ..tools import prompt as prompt_tool
from ..tools.llm import LLMResult, StructuredCaller
from ..util import now_iso

logger = logging.getLogger("xhs_agent.context_state")

TASK_ID = "context_extract"
#: 旧会话恢复时的历史扫描上限：超过就标 partial（与接口层 `_HISTORY_SCAN_LIMIT` 同口径）
RECOVERY_SCAN_LIMIT = 200
#: 审计里保存的原始增量最多几条（超出只留裁决，不撑爆审计行）
MAX_AUDIT_OPERATIONS = 40

#: 抽取前的预算检查回调（S7.26）：`(system, user, output_reserve) -> None`，不通过时抛错
Precheck = Callable[[str, str, int], Awaitable[None]]


@dataclass
class StoredContext:
    """会话行 + 它的状态快照（读一次，按版本用）。"""

    session_id: uuid.UUID
    state_version: int
    topic_id: str | None
    snapshot: ContextSnapshot
    artifact_heads: dict[str, Any] = field(default_factory=dict)
    recovery_status: str = "complete"


@dataclass
class ApplyResult:
    """一次抽取 / 动作的应用结果。"""

    applied: bool = False
    conflict: bool = False
    version: int = 0
    event_type: str = "delta"
    decisions: list[dict[str, Any]] = field(default_factory=list)
    snapshot: ContextSnapshot | None = None
    reason: str = ""
    pending: int = 0
    blocking: int = 0


def _parse_snapshot(row: ChatSession) -> ContextSnapshot:
    return ContextSnapshot.from_dict(
        row.context_state,
        state_version=int(row.state_version or 0),
        topic_id=row.topic_id,
        recovery_status=str(row.recovery_status or "complete"),
    )


async def load(session: AsyncSession, session_id: uuid.UUID) -> StoredContext | None:
    """读会话状态；会话不存在返回 None。坏快照降级成 `partial`（不 500）。"""
    row = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    snapshot = _parse_snapshot(row)
    return StoredContext(
        session_id=session_id,
        state_version=int(row.state_version or 0),
        topic_id=row.topic_id,
        snapshot=snapshot,
        artifact_heads=dict(row.artifact_heads or {}),
        recovery_status=str(row.recovery_status or "complete"),
    )


async def extract(
    cfg: AppConfig,
    caller: StructuredCaller,
    *,
    user_text: str,
    stored: StoredContext,
    refs: list[dict[str, Any]] | None = None,
    precheck: Precheck | None = None,
) -> tuple[ContextDelta, LLMResult]:
    """调用 `context_extract` 抽取增量（输出上限 2048，最多修复一次）。

    `precheck(system, user, output_reserve)` 在**真正发请求前**执行（S7.26：抽取及其修复
    必须走同一个预算入口）；不通过时由回调抛 `BudgetError`，本次抽取不发出。

    失败抛 `PromptError` / `LLMError`，由调用方按"抽取失败"收尾（不静默继续依赖新状态）。
    """
    rendered = prompt_tool.render(
        TASK_ID,
        user_text=_clip_for_prompt(user_text),
        state=context_rules.render_state(stored.snapshot),
        refs=_render_refs(refs or []),
    )
    if precheck is not None:
        await precheck(rendered.system, rendered.user, int(cfg.context.extract_max_tokens))
    delta, result = await caller.call(
        TASK_ID,
        rendered.system,
        rendered.user,
        parse=_parse_delta,
        context={"session_version": stored.state_version},
        json_mode=True,
        max_tokens=cfg.context.extract_max_tokens,
    )
    return delta, result


def _clip_for_prompt(text: str, limit: int = 4000) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def _render_refs(refs: list[dict[str, Any]]) -> str:
    import json

    items = []
    for item in refs[:20]:
        items.append({
            key: item.get(key)
            for key in ("run_id", "position", "object_key", "version_id", "version_no",
                        "material_id", "hotspot_id")
            if item.get(key) not in (None, "")
        })
    return json.dumps(items, ensure_ascii=False)


def _parse_delta(payload: Any) -> ContextDelta:
    if not isinstance(payload, dict):
        raise SchemaError("状态增量必须是 JSON 对象")
    return ContextDelta.from_dict(payload)


async def apply_delta(
    session: AsyncSession,
    cfg: AppConfig,
    stored: StoredContext,
    delta: ContextDelta,
    *,
    message_id: str,
    turn_id: str = "",
    user_text: str,
    mode: str = "enforce",
    refs: list[dict[str, Any]] | None = None,
) -> ApplyResult:
    """把增量应用到会话状态（CAS + 审计同事务）。

    `mode != enforce` 时只做**候选判定**（不写状态、不写审计），供 shadow 阶段观察误判率。
    """
    version = stored.state_version + 1
    topic_id = stored.topic_id or context_rules.new_topic_id()
    result = context_rules.apply_delta(
        stored.snapshot,
        delta,
        user_text=user_text,
        message_id=message_id,
        version=version,
        topic_id=topic_id,
        refs=refs or [],
    )
    blocking = len(
        context_rules.blocking_proposals(result.snapshot, created_version=version)
    )
    if mode != "enforce":
        await _observe_decision(
            session,
            stored=stored,
            event_type="delta",
            actor="extract",
            version=stored.state_version,
            decisions=result.decisions,
            reason="shadow_mode",
            turn_id=turn_id,
            applied=False,
        )
        return ApplyResult(
            applied=False,
            version=stored.state_version,
            decisions=result.decisions,
            snapshot=stored.snapshot,
            reason="shadow_mode",
            pending=result.pending,
            blocking=blocking,
        )
    if not result.changed:
        return ApplyResult(
            applied=False,
            version=stored.state_version,
            decisions=result.decisions,
            snapshot=stored.snapshot,
            reason="no_change",
            blocking=blocking,
        )
    return await _commit(
        session,
        stored=stored,
        snapshot=result.snapshot,
        expected_version=stored.state_version,
        new_version=version,
        topic_id=topic_id,
        event_type="delta",
        actor="extract",
        source_ref={"message_id": message_id, "turn_id": turn_id},
        decisions=result.decisions,
        delta={
            "operations": [
                item.model_dump() for item in delta.operations[:MAX_AUDIT_OPERATIONS]
            ],
            "proposals": delta.proposals[:MAX_AUDIT_OPERATIONS],
        },
        reason="",
        turn_id=turn_id,
        pending=result.pending,
        blocking=blocking,
    )


async def perform_action(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    action: str,
    target_id: str,
    value: str,
    expected_state_version: int,
) -> tuple[ApplyResult, StoredContext]:
    """执行用户动作（confirm / reject / retract / replace / reset_topic）。

    - 会话不存在 → 404；
    - `expected_state_version` 与当前版本不一致 → 409（响应带当前版本，前端刷新后重试）；
    - 目标不存在 / 动作不适用 → `noop`（不推进版本，不写审计）。
    """
    if action not in CONTEXT_ACTIONS:
        raise BadRequestError(f"未知的上下文动作：{action}")
    if action != "reset_topic" and not str(target_id or "").strip():
        raise BadRequestError("这个动作需要 target_id（事实或提案标识）")
    if action == "replace" and not str(value or "").strip():
        raise BadRequestError("replace 需要 value（替换后的新值）")

    stored = await load(session, session_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": str(session_id)})
    if int(expected_state_version) != int(stored.state_version):
        raise ConflictError(
            "上下文状态已变化，请刷新后重试",
            {
                "state_version": stored.state_version,
                "expected_state_version": int(expected_state_version),
                "retryable": True,
            },
        )
    new_version = stored.state_version + 1
    topic_id = stored.topic_id or context_rules.new_topic_id()
    result = context_rules.apply_action(
        stored.snapshot,
        action=action,
        target_id=target_id,
        value=value,
        topic_id=topic_id,
        version=new_version,
    )
    if result.decision == "noop":
        return (
            ApplyResult(
                applied=False,
                version=stored.state_version,
                event_type=action,
                snapshot=stored.snapshot,
                reason=result.reason,
            ),
            stored,
        )
    applied = await _commit(
        session,
        stored=stored,
        snapshot=result.snapshot,
        expected_version=stored.state_version,
        new_version=new_version,
        topic_id=result.snapshot.topic_id or topic_id,
        event_type=result.event_type,
        actor="user",
        source_ref={
            "action_id": f"{action}:{target_id or 'topic'}",
            "expected_state_version": int(expected_state_version),
        },
        decisions=[
            {"fact_id": target_id, "kind": "", "decision": result.event_type,
             "reason": result.reason}
        ],
        delta={"value": value} if value else {},
        reason=result.reason,
        turn_id="",
    )
    latest = await load(session, session_id)
    assert latest is not None
    return applied, latest


async def _commit(
    session: AsyncSession,
    *,
    stored: StoredContext,
    snapshot: ContextSnapshot,
    expected_version: int,
    new_version: int,
    topic_id: str,
    event_type: str,
    actor: str,
    source_ref: dict[str, Any],
    decisions: list[dict[str, Any]],
    delta: dict[str, Any],
    reason: str,
    turn_id: str,
    pending: int = 0,
    blocking: int = 0,
) -> ApplyResult:
    """条件更新快照 + 写审计（同一事务）；失败不留半个状态。"""
    snapshot.state_version = new_version
    snapshot.topic_id = topic_id
    statement = (
        update(ChatSession)
        .where(ChatSession.id == stored.session_id,
               ChatSession.state_version == int(expected_version))
        .values(
            state_version=int(new_version),
            topic_id=topic_id,
            context_state=snapshot.to_dict(),
            recovery_status=snapshot.recovery_status,
        )
    )
    cursor = cast(CursorResult[Any], await session.execute(statement))
    if int(cursor.rowcount or 0) == 0:
        await session.rollback()
        current = await load(session, stored.session_id)
        latest_version = current.state_version if current else expected_version
        return ApplyResult(
            applied=False,
            conflict=True,
            version=latest_version,
            event_type=event_type,
            decisions=decisions,
            snapshot=current.snapshot if current else stored.snapshot,
            reason="state_version_conflict",
            pending=pending,
            blocking=blocking,
        )
    session.add(
        ChatContextEvent(
            session_id=stored.session_id,
            state_version=int(new_version),
            event_type=event_type,
            actor=actor,
            source_ref=source_ref,
            decisions=decisions[:MAX_AUDIT_OPERATIONS],
            delta=delta,
            reason=reason,
        )
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await _observe_decision(
        session,
        stored=stored,
        event_type=event_type,
        actor=actor,
        version=new_version,
        decisions=decisions,
        reason=reason,
        turn_id=turn_id,
        applied=True,
    )
    return ApplyResult(
        applied=True,
        version=new_version,
        event_type=event_type,
        decisions=decisions,
        snapshot=snapshot,
        reason=reason,
        pending=pending,
        blocking=blocking,
    )


async def _observe_decision(
    session: AsyncSession,
    *,
    stored: StoredContext,
    event_type: str,
    actor: str,
    version: int,
    decisions: list[dict[str, Any]],
    reason: str,
    turn_id: str,
    applied: bool,
) -> None:
    """`context_state_decision` 观测：只记裁决与版本，不记用户原文。"""
    counts: dict[str, int] = {}
    for item in decisions:
        key = str(item.get("decision") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    await record_observation(
        session,
        event_type="context_state_decision",
        session_id=stored.session_id,
        key=f"{event_type}:{version}:{actor}:{applied}:{reason}:{len(decisions)}",
        payload={
            "event_type": event_type,
            "actor": actor,
            "state_version": version,
            "applied": applied,
            "reason": reason,
            "counts": counts,
            "decisions": decisions[:20],
        },
        turn_id=turn_id,
        policy_version="context_v1",
    )


# ---------------------------------------------------------------------------
# 旧会话恢复
# ---------------------------------------------------------------------------


async def assess_recovery(
    session: AsyncSession, session_id: uuid.UUID, *, scan_limit: int = RECOVERY_SCAN_LIMIT
) -> tuple[str, str]:
    """判定旧会话恢复是否完整；返回 `(status, reason)`，必要时写回会话行。

    partial 的三种来源（方案 §4.3）：历史扫描上限导致前文缺失、来源消息已删除、
    证据在该消息里找不到。**不从旧摘要猜回约束**，也不把"最近记录里没有"当成"从未设置"。
    """
    stored = await load(session, session_id)
    if stored is None:
        return "complete", ""
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(ChatMessage).where(
                    ChatMessage.session_id == session_id
                )
            )
        ).scalar_one()
    )
    reason = ""
    if total >= scan_limit:
        reason = "history_scan_truncated"
    else:
        for fact in stored.snapshot.active():
            message_id = str(fact.source_ref.get("message_id") or "")
            if not message_id:
                continue
            try:
                row = (
                    await session.execute(
                        select(ChatMessage.content).where(
                            ChatMessage.id == uuid.UUID(message_id),
                            ChatMessage.session_id == session_id,
                        )
                    )
                ).scalar_one_or_none()
            except ValueError:
                row = None
            if row is None:
                reason = "evidence_unavailable"
                break
            if fact.evidence and "".join(fact.evidence.split()) not in "".join(
                str(row).split()
            ):
                reason = "evidence_unavailable"
                break
    status = "partial" if reason else "complete"
    if status != stored.recovery_status:
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(recovery_status=status)
        )
        await session.commit()
        stored.recovery_status = status
    return status, reason


async def mark_partial(
    session: AsyncSession, session_id: uuid.UUID, *, reason: str
) -> bool:
    """显式把会话标成 `partial`（历史扫描不完整 / 恢复失败时用）；返回是否改动。"""
    result = await session.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.recovery_status != "partial")
        .values(recovery_status="partial")
    )
    cursor = cast(CursorResult[Any], result)
    if int(cursor.rowcount or 0):
        await session.commit()
        await record_observation(
            session,
            event_type="context_state_decision",
            session_id=session_id,
            key=f"recovery:{reason}",
            payload={"event_type": "recovery", "reason": reason, "status": "partial"},
            policy_version="context_v1",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


def context_view(
    cfg: AppConfig, stored: StoredContext, *, blocking: int | None = None
) -> dict[str, Any]:
    """接口 / 装配器用的上下文视图（结构见 openapi `ChatContext`）。"""
    heads = []
    for object_key, raw in sorted((stored.artifact_heads or {}).items()):
        if not isinstance(raw, dict):
            continue
        heads.append(
            {
                "object_key": object_key,
                "version_id": str(raw.get("version_id") or ""),
                "version_no": int(raw.get("version_no") or 0),
                "updated_at": str(raw.get("updated_at") or ""),
            }
        )
    blockers = (
        len(context_rules.blocking_proposals(stored.snapshot))
        if blocking is None
        else int(blocking)
    )
    return {
        "session_id": str(stored.session_id),
        "state_version": int(stored.state_version),
        "topic_id": stored.topic_id,
        "recovery_status": stored.recovery_status,
        "facts": [item.to_dict() for item in stored.snapshot.facts
                  if item.status in ("active", "pending_confirmation")],
        "proposals": [item.to_dict() for item in stored.snapshot.proposals],
        "artifact_heads": heads,
        "effective_mode": cfg.context.effective_mode(str(stored.session_id)),
        "blocking_proposals": blockers,
    }


def state_prompt(stored: StoredContext, *, limit: int = 12) -> str:
    """装配给 supervisor 的状态段（放在固定前缀之后，不能为了命中缓存保留过期事实）。"""
    facts = stored.snapshot.active()[:limit]
    if not facts:
        return ""
    lines = ["[当前状态] 用户明确表达、且已有原文证据的要求："]
    for fact in facts:
        scope = "整个会话" if fact.scope == "session" else (
            f"当前话题（{fact.scope_id}）" if fact.scope == "topic" else f"产物 {fact.scope_id}"
        )
        lines.append(f"- {fact.kind}：{fact.value}（作用域：{scope}）")
    proposals = stored.snapshot.pending()[:5]
    if proposals:
        lines.append("[待确认] 以下要求尚未生效，不要按它执行：")
        for proposal in proposals:
            lines.append(f"- {proposal.kind}：{proposal.value}（{proposal.reason}）")
    return "\n".join(lines)


def conflict_detail(stored: StoredContext) -> dict[str, Any]:
    return {
        "state_version": stored.state_version,
        "retryable": True,
        "updated_at": now_iso(),
    }


def objects_for_refs(
    run_id: str, hotspots: list[dict[str, Any]], *, positions: list[int] | None = None
) -> list[dict[str, Any]]:
    """把一次运行的热点位置转成"已验证引用"（供抽取 prompt 与版本服务共用）。"""
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(hotspots, start=1):
        if positions is not None and index not in positions:
            continue
        refs.append(
            {
                "run_id": run_id,
                "position": index,
                "object_key": f"draft:{run_id}:{index}",
                "hotspot_id": str(item.get("hotspot_id") or ""),
                "title": str(item.get("summary") or item.get("hotspot_raw") or "")[:40],
            }
        )
    return refs


__all__ = [
    "ApplyResult",
    "StoredContext",
    "apply_delta",
    "assess_recovery",
    "context_view",
    "extract",
    "load",
    "mark_partial",
    "objects_for_refs",
    "perform_action",
    "state_prompt",
]

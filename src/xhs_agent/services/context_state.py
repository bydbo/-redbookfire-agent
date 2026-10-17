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

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core import budget as budget_tool
from ..core import context_rules
from ..core.errors import BadRequestError, ConflictError, NotFoundError
from ..core.observations import record as record_observation
from ..db.models import (
    ChatContextAction,
    ChatContextEvent,
    ChatMessage,
    ChatSession,
)
from ..schemas import (
    CONTEXT_ACTIONS,
    ContextDelta,
    ContextSnapshot,
    HotspotClue,
    SchemaError,
)
from ..tools import prompt as prompt_tool
from ..tools.llm import LLMError, LLMResult, StructuredCaller
from ..tools.prompt import PromptError
from ..util import now_iso
from .chat import update_message

logger = logging.getLogger("xhs_agent.context_state")

TASK_ID = "context_extract"
RECOVERY_TASK_ID = "context_recover"
#: 旧会话恢复时的历史扫描上限：超过就标 partial（与接口层 `_HISTORY_SCAN_LIMIT` 同口径）
RECOVERY_SCAN_LIMIT = 200
#: 审计里保存的原始增量最多几条（超出只留裁决，不撑爆审计行）
MAX_AUDIT_OPERATIONS = 40

#: 抽取前的预算检查回调（S7.26）：`(system, user, output_reserve) -> None`，不通过时抛错
Precheck = Callable[[str, str, int], Awaitable[None]]


@dataclass
class StoredContext:
    """会话行 + 它的状态快照（读一次，按版本用）。

    `recovery` 是恢复进度（v2 §3.5）：`status` ∈ complete / pending / running /
    awaiting_confirmation / interrupted / failed。旧会话没有恢复记录且已有用户消息时
    判为 `pending`（E7CTX-05：不能因事实列表为空判为完整）。
    """

    session_id: uuid.UUID
    state_version: int
    topic_id: str | None
    snapshot: ContextSnapshot
    artifact_heads: dict[str, Any] = field(default_factory=dict)
    recovery_status: str = "complete"
    recovery: dict[str, Any] = field(default_factory=dict)


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
    raw_state = dict(row.context_state or {})
    recovery = dict(raw_state.get("recovery") or {})
    if not recovery:
        # E7CTX-05：区分新空会话与未恢复的旧会话——已有用户历史却没有恢复记录时
        # 判为 pending，不能因事实列表为空返回"完整但零事实"。
        user_messages = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.session_id == session_id,
                           ChatMessage.role == "user")
                )
            ).scalar_one()
        )
        recovery = (
            {"status": "pending", "reason": "legacy_unrecovered"}
            if user_messages > 0
            else {"status": "complete", "reason": "new_session"}
        )
    return StoredContext(
        session_id=session_id,
        state_version=int(row.state_version or 0),
        topic_id=row.topic_id,
        snapshot=snapshot,
        artifact_heads=dict(row.artifact_heads or {}),
        recovery_status=str(row.recovery_status or "complete"),
        recovery=recovery,
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
    verify_source: bool = True,
) -> ApplyResult:
    """把增量应用到会话状态（CAS + 审计同事务）。

    `mode != enforce` 时只做**候选判定**（不写状态、不写审计），供 shadow 阶段观察误判率。
    来源校验（§2.1）：来源消息必须真实存在、属于当前会话且角色为 user；不通过时整批
    不提交（`status=invalid`），依赖工具不得启动。
    """
    if verify_source:
        source_row = (
            await session.execute(
                select(ChatMessage).where(
                    ChatMessage.id == uuid.UUID(str(message_id)),
                    ChatMessage.session_id == stored.session_id,
                )
            )
        ).scalar_one_or_none()
        if source_row is None or str(source_row.role) != "user":
            return ApplyResult(
                applied=False,
                version=stored.state_version,
                event_type="delta",
                snapshot=stored.snapshot,
                reason="source_message_invalid",
            )
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
    blocking = len(context_rules.blocking_proposals(result.snapshot))
    if result.invalid:
        # E7CTX-03：整批验证失败 → 不提交、写一条观测，调用方停止依赖任务
        await _observe_decision(
            session,
            stored=stored,
            event_type="delta",
            actor="extract",
            version=stored.state_version,
            decisions=result.decisions,
            reason=result.invalid_reason or "invalid_delta",
            turn_id=turn_id,
            applied=False,
        )
        return ApplyResult(
            applied=False,
            version=stored.state_version,
            decisions=result.decisions,
            snapshot=stored.snapshot,
            reason="invalid_delta",
            pending=0,
            blocking=blocking,
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
    action_id: str | None = None,
    object_ref: dict[str, Any] | None = None,
    recovery_id: str | None = None,
    retry_of: str | None = None,
    acknowledge_partial_history: bool = False,
    request_payload: dict[str, Any] | None = None,
    cfg: AppConfig | None = None,
    caller: StructuredCaller | None = None,
) -> dict[str, Any]:
    """执行用户动作并返回**动作结果视图**（含 action_id / accounting_message_id）。

    幂等（v2 §3.3A）：`action_id` 是客户端生成的重放键——同 (session, action_id) 且同
    请求体重放返回原动作结果，不同请求体返回 409；先查重放记录，再做 CAS。
    - 会话不存在 → 404；`expected_state_version` 不一致 → 409；
    - 确认替换时旧项已变化 → 409（E7CTX-04，不以 200 包装）；
    - 目标不存在 / 动作不适用 → `noop`（不推进版本、不写审计，但记录动作结果）。
    """
    if action not in CONTEXT_ACTIONS:
        raise BadRequestError(f"未知的上下文动作：{action}")
    needs_target = action in ("confirm", "reject", "retract", "replace")
    if needs_target and not str(target_id or "").strip():
        raise BadRequestError("这个动作需要 target_id（事实或提案标识）")
    if action == "replace" and not str(value or "").strip():
        raise BadRequestError("replace 需要 value（替换后的新值）")
    if action == "select_object" and not object_ref:
        raise BadRequestError("select_object 需要 object_ref（对象引用）")
    if action == "select_object" and target_id:
        raise BadRequestError("select_object 不接受 target_id")

    resolved_action_id = uuid.UUID(str(action_id)) if action_id else uuid.uuid4()
    digest = _action_digest(action, expected_state_version, target_id, value,
                            object_ref, recovery_id, retry_of,
                            acknowledge_partial_history, request_payload)
    claim = await _claim_action(
        session, session_id, action_id=resolved_action_id, action=action,
        request_digest=digest, expected_state_version=expected_state_version,
        retry_of=retry_of,
    )
    if claim.replay:
        row = claim.row
        assert row is not None
        stored = await load(session, session_id)
        assert stored is not None
        return _action_result_view(
            session_id=session_id, stored=stored, action=action,
            action_id=resolved_action_id, row=row, replay=True,
        )

    if action == "recover":
        if cfg is None or caller is None:
            raise BadRequestError("恢复需要模型调用配置（cfg 与 caller）")
        return await _run_recovery_action(
            session, session_id, cfg=cfg, caller=caller,
            action_row=claim.row, action_id=resolved_action_id,
            recovery_id=recovery_id, retry_of=retry_of,
            expected_state_version=int(expected_state_version),
        )

    stored = await load(session, session_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": str(session_id)})
    if int(expected_state_version) != int(stored.state_version):
        raise ConflictError(
            "上下文状态已变化，请刷新后重试",
            {
                "reason": "state_version_conflict",
                "state_version": stored.state_version,
                "expected_state_version": int(expected_state_version),
                "retryable": True,
            },
        )
    new_version = stored.state_version + 1
    topic_id = stored.topic_id or context_rules.new_topic_id()

    if action == "select_object":
        validated = await _validate_object_ref(
            session, session_id, dict(object_ref or {})
        )
        result = context_rules.apply_action(
            stored.snapshot, action="select_object",
            value=validated,   # 已是 JSON 字符串（_validate_object_ref 返回）
            topic_id=topic_id, version=new_version,
        )
    elif action == "complete_recovery":
        outcome = await _apply_complete_recovery(
            session, stored,
            acknowledge_partial_history=bool(acknowledge_partial_history),
        )
        result = outcome
    else:
        result = context_rules.apply_action(
            stored.snapshot,
            action=action,
            target_id=target_id,
            value=value,
            topic_id=topic_id,
            version=new_version,
        )
    if result.decision == "noop":
        row = await _finish_action(
            session, claim.row, status="succeeded",
            result={"decision": "noop", "reason": result.reason},
        )
        return _action_result_view(
            session_id=session_id, stored=stored, action=action,
            action_id=resolved_action_id, row=row,
        )
    if result.decision == "conflict":
        # E7CTX-04：确认替换时旧项已变化——409，不留下半状态
        await _finish_action(
            session, claim.row, status="failed",
            error={"reason": result.reason, "retryable": False},
        )
        raise ConflictError(
            "确认的替换目标已变化，请刷新后重新确认",
            {
                "reason": result.reason,
                "state_version": stored.state_version,
                "retryable": True,
            },
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
            "action_id": str(resolved_action_id),
            "expected_state_version": int(expected_state_version),
        },
        decisions=[
            {"fact_id": target_id, "kind": "", "decision": result.event_type,
             "reason": result.reason}
        ],
        delta={"value": value} if isinstance(value, str) and value else
              ({"object_ref": object_ref} if object_ref else {}),
        reason=result.reason,
        turn_id="",
    )
    if applied.conflict:
        # 提交阶段 CAS 竞争：409（E7CTX-03），不能以 200 包装成普通无操作
        await _finish_action(
            session, claim.row, status="failed",
            error={"reason": "state_version_conflict", "retryable": True},
        )
        raise ConflictError(
            "上下文状态已变化，请刷新后重试",
            {
                "reason": "state_version_conflict",
                "state_version": applied.version,
                "retryable": True,
            },
        )
    latest = await load(session, session_id)
    assert latest is not None
    row = await _finish_action(
        session, claim.row, status="succeeded",
        result={"decision": "applied", "state_version": applied.version},
        expected_state_version=int(expected_state_version),
    )
    return _action_result_view(
        session_id=session_id, stored=latest, action=action,
        action_id=resolved_action_id, row=row,
    )


def _action_digest(
    action: str,
    expected_state_version: int,
    target_id: str,
    value: Any,
    object_ref: dict[str, Any] | None,
    recovery_id: str | None,
    retry_of: str | None,
    acknowledge_partial_history: bool,
    request_payload: dict[str, Any] | None,
) -> str:
    body = json.dumps(
        {
            "action": action,
            "expected_state_version": int(expected_state_version),
            "target_id": str(target_id or ""),
            "value": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            "object_ref": object_ref or {},
            "recovery_id": str(recovery_id or ""),
            "retry_of": str(retry_of or ""),
            "acknowledge_partial_history": bool(acknowledge_partial_history),
            "request_payload": request_payload or {},
        },
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _claim_action(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    action_id: uuid.UUID,
    action: str,
    request_digest: str,
    expected_state_version: int,
    retry_of: str | None,
) -> Any:
    """动作认领：同 (session, action_id) 已存在时按请求体判定重放或冲突（§3.3A）。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    statement = (
        pg_insert(ChatContextAction)
        .values(
            session_id=session_id,
            action_id=action_id,
            action=action,
            request_digest=request_digest,
            expected_state_version=int(expected_state_version),
            status="running",
            retry_of=uuid.UUID(str(retry_of)) if retry_of else None,
        )
        .on_conflict_do_nothing(constraint="uq_chat_context_actions_session_action")
        .returning(ChatContextAction.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        await session.commit()
        row = (
            await session.execute(
                select(ChatContextAction).where(ChatContextAction.id == inserted)
            )
        ).scalar_one()
        return SimpleNamespace(replay=False, row=row)
    existing = (
        await session.execute(
            select(ChatContextAction).where(
                ChatContextAction.session_id == session_id,
                ChatContextAction.action_id == action_id,
            )
        )
    ).scalar_one()
    if str(existing.request_digest) != request_digest:
        raise ConflictError(
            "相同 action_id 携带了不同的请求体",
            {"reason": "action_id_reuse", "action_id": str(action_id), "retryable": False},
        )
    return SimpleNamespace(replay=True, row=existing)


async def _finish_action(
    session: AsyncSession,
    row: ChatContextAction,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    expected_state_version: int | None = None,
    accounting_message_id: uuid.UUID | None = None,
    recovery_id: uuid.UUID | None = None,
) -> ChatContextAction:
    row.status = status
    if result is not None:
        row.result = dict(result)
    if error is not None:
        row.error = dict(error)
    if expected_state_version is not None:
        row.expected_state_version = int(expected_state_version)
    if accounting_message_id is not None:
        row.accounting_message_id = accounting_message_id
    if recovery_id is not None:
        row.recovery_id = recovery_id
    row.updated_at = _utcnow()
    await session.commit()
    return row


def _utcnow() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _action_result_view(
    *,
    session_id: uuid.UUID,
    stored: StoredContext,
    action: str,
    action_id: uuid.UUID,
    row: ChatContextAction,
    replay: bool = False,
) -> dict[str, Any]:
    decision = str((row.result or {}).get("decision") or "")
    if row.status == "failed":
        decision = "conflict"
    elif not decision:
        decision = "noop"
    return {
        "session_id": str(session_id),
        "state_version": int(stored.state_version),
        "action": action,
        "action_id": str(action_id),
        "decision": decision,
        "reason": str((row.error or {}).get("reason") or (row.result or {}).get("reason") or ""),
        # context 由调用方（路由层）用 cfg 补齐后返回
        "accounting_message_id": str(row.accounting_message_id)
        if row.accounting_message_id else None,
        "replay": replay,
        "status": str(row.status),
    }
RECOVERY_PAGE_SIZE = 20
MAX_RAW_TEXT_CHARS = 500
CLUE_EDITABLE_FIELDS = frozenset({
    "raw_text", "titles", "body", "tags", "cover_text", "first_3s",
    "shot_list", "compliance_notes", "topic", "scene", "visual", "emotion",
    "sound", "conflict", "format", "audience",
})


async def validate_operation_binding(
    session: AsyncSession, session_id: uuid.UUID, raw: str | None
) -> dict[str, Any] | None:
    """解析并校验 multipart 里的结构化操作绑定（v2 §3.3B）；无绑定时返回 None。

    校验口径：`operation` 取 revise_draft / parse_clue_from_image / run_hotspot_analysis；
    版本引用必须是 UUID；按操作拒绝不适用组合（改稿不能夹带重解析；force_reparse 不能
    同时要求读取旧 clue 版本）；`expected_state_version` 与当前版本一致才受理
    （接收时检查一次；本轮状态增量成功后认领使用服务端接受的新版本）。
    """
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise BadRequestError("operation_binding 不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise BadRequestError("operation_binding 必须是 JSON 对象")
    operation = str(body.get("operation") or "")
    if operation not in ("revise_draft", "parse_clue_from_image", "run_hotspot_analysis"):
        raise BadRequestError(f"不支持的绑定操作：{operation}")
    force_reparse = bool(body.get("force_reparse") or False)
    clue_version_id = str(body.get("clue_version_id") or "")
    base_version_id = str(body.get("base_version_id") or "")
    object_key = str(body.get("object_key") or "")
    expected = body.get("expected_state_version")
    if operation == "revise_draft" and force_reparse:
        raise BadRequestError("改稿绑定不能夹带 force_reparse")
    if force_reparse and clue_version_id:
        raise BadRequestError("force_reparse 不能同时要求读取旧 clue 版本")
    for name, value in (("base_version_id", base_version_id),
                        ("clue_version_id", clue_version_id)):
        if value:
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise BadRequestError(f"{name} 不是合法 UUID") from exc
    if expected is None:
        raise BadRequestError("绑定必须携带 expected_state_version")
    stored = await load(session, session_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": str(session_id)})
    if int(expected) != int(stored.state_version):
        raise ConflictError(
            "绑定的预期状态版本已过期，请刷新后重试",
            {"reason": "state_version_conflict", "state_version": stored.state_version,
             "retryable": True},
        )
    if object_key and object_key.startswith("draft:"):
        await _validate_object_ref(session, session_id, {"object_key": object_key})
    binding = {**body, "expected_state_version": int(expected)}
    return binding


async def edit_clue_version(
    session: AsyncSession,
    session_id: uuid.UUID,
    cfg: AppConfig,
    *,
    action_id: str,
    expected_state_version: int,
    expected_head_version_id: str | None,
    object_key: str,
    base_version_id: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """保存用户编辑的线索版本（v2 §3.3C）：独立不可变业务版本 + 选定引用，同事务推进。"""
    from . import artifact_versions

    if not object_key.startswith("clue:"):
        raise BadRequestError("线索编辑的对象键必须以 clue: 开头")
    raw_text = str(content.get("raw_text") or "").strip()
    if not raw_text or len(raw_text) > MAX_RAW_TEXT_CHARS:
        raise BadRequestError(f"raw_text 必填且 ≤ {MAX_RAW_TEXT_CHARS} 字")
    edited_fields = [str(x) for x in (content.get("edited_fields") or [])]
    invalid_fields = [x for x in edited_fields if x not in CLUE_EDITABLE_FIELDS]
    if invalid_fields:
        raise BadRequestError(f"edited_fields 含不允许的字段：{invalid_fields}")
    try:
        clue = HotspotClue.from_dict(dict(content.get("clue") or {}), hotspot_raw=raw_text)
    except SchemaError as exc:
        raise BadRequestError(f"线索不符合契约：{exc}") from exc
    stored = await load(session, session_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": str(session_id)})
    if int(expected_state_version) != int(stored.state_version):
        raise ConflictError(
            "上下文状态已变化，请刷新后重试",
            {"reason": "state_version_conflict", "state_version": stored.state_version,
             "retryable": True},
        )
    resolved_action_id = uuid.UUID(str(action_id)) if action_id else uuid.uuid4()
    digest = _action_digest("edit_clue", expected_state_version, object_key,
                            json.dumps(content, ensure_ascii=False, sort_keys=True),
                            None, None, None, False, None)
    claim = await _claim_action(
        session, session_id, action_id=resolved_action_id, action="edit_clue",
        request_digest=digest, expected_state_version=expected_state_version,
        retry_of=None,
    )
    if claim.replay:
        view = _action_result_view(
            session_id=session_id, stored=stored, action="edit_clue",
            action_id=resolved_action_id, row=claim.row, replay=True,
        )
        view["decision"] = "applied"
        return view

    try:
        base_row = await artifact_versions.get_version(
            session, session_id, uuid.UUID(str(base_version_id))
        )
    except ValueError as exc:
        raise BadRequestError("base_version_id 不是合法 UUID") from exc
    if base_row is None or str(base_row.object_key) != object_key \
            or str(base_row.status) != "succeeded":
        raise BadRequestError("基础版本不属于该线索对象（或不是成功版本）")
    head = await artifact_versions.head(session, session_id, object_key)
    expected_head = uuid.UUID(str(expected_head_version_id)) if expected_head_version_id else (
        head.id if head is not None else None
    )
    artifact_claim = await artifact_versions.claim(
        session,
        session_id=session_id,
        object_key_value=object_key,
        object_type="clue",
        run_id=None,
        position=None,
        request_key_value=f"edit:{resolved_action_id}",
        source={"actor": "clue_edit", "action_id": str(resolved_action_id)},
        base_version_id=base_row.id,
        expected_head_version_id=expected_head,
        expected_state_version=int(expected_state_version),
    )
    if not artifact_claim.is_new and artifact_claim.status == "succeeded" \
            and artifact_claim.row is not None:
        row = await _finish_action(
            session, claim.row, status="succeeded",
            result={"decision": "applied", "version_id": str(artifact_claim.row.id)},
        )
        view = _action_result_view(
            session_id=session_id, stored=stored, action="edit_clue",
            action_id=resolved_action_id, row=row, replay=True,
        )
        view["decision"] = "applied"
        return view
    assert artifact_claim.row is not None
    try:
        committed = await artifact_versions.commit_success(
            session,
            claim_row=artifact_claim.row,
            content={
                "raw_text": raw_text,
                "clue": clue.to_dict(),
                "edited_fields": edited_fields,
            },
            usage={},
            expected_state_version=int(expected_state_version),
        )
    except ConflictError:
        await artifact_versions.finish(session, artifact_claim.row, status="failed",
                                       error="head_changed_during_generation")
        raise
    # 成功后设为选定引用（同事务推进状态版本并写用户动作审计）
    new_version = stored.state_version + 1
    result = context_rules.apply_action(
        stored.snapshot, action="select_object",
        value=json.dumps({"object_key": object_key, "object_type": "clue",
                          "version_id": str(committed.id)}, ensure_ascii=False),
        topic_id=stored.topic_id or context_rules.new_topic_id(),
        version=new_version,
    )
    applied = await _commit(
        session, stored=stored, snapshot=result.snapshot,
        expected_version=stored.state_version, new_version=new_version,
        topic_id=stored.topic_id or context_rules.new_topic_id(),
        event_type="select_object", actor="user",
        source_ref={"action_id": str(resolved_action_id), "phase": "clue_edit_select"},
        decisions=[{"fact_id": "", "kind": "", "decision": "select_object",
                    "reason": "clue_edit"}],
        delta={"object_key": object_key, "version_id": str(committed.id)},
        reason="clue_edit", turn_id="",
    )
    if applied.conflict:
        raise ConflictError(
            "状态版本已变化，选定引用未写入",
            {"reason": "state_version_conflict", "state_version": applied.version,
             "retryable": True},
        )
    final = await load(session, session_id)
    assert final is not None
    row = await _finish_action(
        session, claim.row, status="succeeded",
        result={"decision": "applied", "version_id": str(committed.id)},
        expected_state_version=int(expected_state_version),
    )
    view = _action_result_view(
        session_id=session_id, stored=final, action="edit_clue",
        action_id=resolved_action_id, row=row,
    )
    view["decision"] = "applied"
    view["version_id"] = str(committed.id)
    return view


async def select_clue_version(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    action_id: str,
    expected_state_version: int,
    version_id: uuid.UUID,
) -> dict[str, Any]:
    """选定一个线索版本（v2 §3.3C）：只选同会话成功 clue 版本；推进状态版本并写审计。"""
    from . import artifact_versions

    row = await artifact_versions.get_version(session, session_id, version_id)
    if row is None or str(row.object_type) != "clue" or str(row.status) != "succeeded":
        raise NotFoundError(f"线索版本不存在：{version_id}",
                            {"session_id": str(session_id), "version_id": str(version_id)})
    resolved_action_id = uuid.UUID(str(action_id)) if action_id else uuid.uuid4()
    digest = _action_digest("select_clue", expected_state_version, str(row.id), "", None,
                            None, None, False, None)
    claim = await _claim_action(
        session, session_id, action_id=resolved_action_id, action="select_clue",
        request_digest=digest, expected_state_version=expected_state_version,
        retry_of=None,
    )
    if claim.replay:
        stored = await load(session, session_id)
        assert stored is not None
        view = _action_result_view(
            session_id=session_id, stored=stored, action="select_clue",
            action_id=resolved_action_id, row=claim.row, replay=True,
        )
        view["decision"] = "applied"
        return view
    stored = await load(session, session_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": str(session_id)})
    if int(expected_state_version) != int(stored.state_version):
        raise ConflictError(
            "上下文状态已变化，请刷新后重试",
            {"reason": "state_version_conflict", "state_version": stored.state_version,
             "retryable": True},
        )
    new_version = stored.state_version + 1
    result = context_rules.apply_action(
        stored.snapshot, action="select_object",
        value=json.dumps({"object_key": str(row.object_key), "object_type": "clue",
                          "version_id": str(row.id)}, ensure_ascii=False),
        topic_id=stored.topic_id or context_rules.new_topic_id(),
        version=new_version,
    )
    applied = await _commit(
        session, stored=stored, snapshot=result.snapshot,
        expected_version=stored.state_version, new_version=new_version,
        topic_id=stored.topic_id or context_rules.new_topic_id(),
        event_type="select_object", actor="user",
        source_ref={"action_id": str(resolved_action_id), "phase": "clue_select"},
        decisions=[{"fact_id": "", "kind": "", "decision": "select_object",
                    "reason": "clue_select"}],
        delta={"object_key": str(row.object_key), "version_id": str(row.id)},
        reason="clue_select", turn_id="",
    )
    if applied.conflict:
        raise ConflictError(
            "状态版本已变化，请刷新后重试",
            {"reason": "state_version_conflict", "state_version": applied.version,
             "retryable": True},
        )
    final = await load(session, session_id)
    assert final is not None
    finished = await _finish_action(
        session, claim.row, status="succeeded",
        result={"decision": "applied", "version_id": str(row.id)},
        expected_state_version=int(expected_state_version),
    )
    view = _action_result_view(
        session_id=session_id, stored=final, action="select_clue",
        action_id=resolved_action_id, row=finished,
    )
    view["decision"] = "applied"
    return view


RECOVERY_PAGE_SIZE = 20


async def _validate_object_ref(
    session: AsyncSession, session_id: uuid.UUID, ref: dict[str, Any]
) -> str:
    """服务端验证对象引用（E7CTX-17）：模型给出的合法 UUID 不等于有效归属。

    - `draft:<run_id>:<position>`：run 必须出现在本会话的历史消息里，position ≥ 1；
    - `clue:...`：图片组键按组摘要读取，交给使用方按附件归属校验；
    - 其他会话的产物版本始终不可引用（这里只接受对象键，不接受跨会话 version_id）。
    """
    object_key = str(ref.get("object_key") or "").strip()
    if not object_key:
        raise BadRequestError("object_ref 缺少 object_key")
    if object_key.startswith("draft:"):
        parts = object_key.split(":")
        if len(parts) != 3:
            raise BadRequestError(f"draft 对象键格式不合法：{object_key}")
        run_id = parts[1]
        try:
            position = int(parts[2])
        except ValueError as exc:
            raise BadRequestError(f"draft 位置不合法：{object_key}") from exc
        if position < 1:
            raise BadRequestError("draft 位置必须 ≥ 1")
        try:
            run_uuid = uuid.UUID(run_id)
        except ValueError as exc:
            raise BadRequestError(f"run_id 不是合法 UUID：{run_id}") from exc
        belongs = (
            await session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == session_id,
                       ChatMessage.run_id == run_uuid)
            )
        ).scalar_one()
        if int(belongs) == 0:
            raise NotFoundError(
                f"run 不属于当前会话：{run_id}",
                {"session_id": str(session_id), "run_id": run_id},
            )
        validated = {**ref, "object_key": object_key, "object_type": "draft",
                     "run_id": run_id, "hotspot_position": position}
        return json.dumps(validated, ensure_ascii=False)
    if object_key.startswith("clue:"):
        validated = {**ref, "object_key": object_key, "object_type": "clue"}
        return json.dumps(validated, ensure_ascii=False)
    raise BadRequestError(f"不支持的对象键：{object_key}")


async def _run_recovery_action(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    cfg: AppConfig,
    caller: StructuredCaller,
    action_row: ChatContextAction,
    action_id: uuid.UUID,
    recovery_id: str | None,
    retry_of: str | None,
    expected_state_version: int,
) -> dict[str, Any]:
    """恢复一页（v2 §3.5）：显式触发、每请求最多 20 条完整用户消息、候选全部待确认。

    - 旧会话首次 recover：锁定历史上界（新增聊天不扩大本次范围）、记录恢复 id；
    - 预算装不下整页 → 请求不发出、游标保持、报告 overflow；
    - 候选验证失败 → 该页不推进，记录失败与消费；
    - 已有 running 认领 / 失败页不自动重试付费调用（retry_of 显式重试除外）。
    """
    stored = await load(session, session_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": str(session_id)})
    recovery = dict(stored.recovery or {})
    status = str(recovery.get("status") or "pending")

    async def _finish(
        *, action_status: str, decision: str, reason: str,
        result_extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = await _finish_action(
            session, action_row, status=action_status,
            result={"decision": decision, "reason": reason,
                    **(result_extra or {})},
            recovery_id=uuid.UUID(str(recovery.get("recovery_id")))
            if recovery.get("recovery_id") else None,
        )
        view = _action_result_view(
            session_id=session_id, stored=stored, action="recover",
            action_id=action_id, row=row,
        )
        view["reason"] = reason
        return view

    if status == "complete":
        return await _finish(action_status="succeeded", decision="noop",
                             reason="recovery_complete")
    if status == "running":
        # 已有 running 认领：不自动重试付费页（§3.5 第 6 条）
        return await _finish(action_status="interrupted", decision="noop",
                             reason="recovery_running")
    if status in ("failed", "interrupted") and not retry_of:
        return await _finish(action_status="interrupted", decision="noop",
                             reason="recovery_retry_required")

    # ---- 初始化或续扫 ----
    upper_id = recovery.get("snapshot_last_message_id")
    if not upper_id:
        upper_row = (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if upper_row is None:
            recovery = {**recovery, "status": "complete", "reason": "no_history"}
            stored.recovery = recovery
            row = await _finish_action(
                session, action_row, status="succeeded",
                result={"decision": "noop", "reason": "no_history"},
                expected_state_version=int(expected_state_version),
            )
            return _action_result_view(
                session_id=session_id, stored=stored, action="recover",
                action_id=action_id, row=row,
            )
        total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.session_id == session_id,
                           ChatMessage.role == "user",
                           ChatMessage.created_at <= upper_row.created_at)
                )
            ).scalar_one()
        )
        recovery = {
            "recovery_id": str(uuid.uuid4()),
            "status": "running",
            "reason": str(recovery.get("reason") or "legacy_unrecovered"),
            "snapshot_last_message_id": str(upper_row.id),
            "snapshot_last_created_at": upper_row.created_at.isoformat(),
            "cursor_created_at": None,
            "cursor_message_id": None,
            "scanned_user_messages": 0,
            "total_user_messages": total,
            "pending_proposal_ids": [],
            "accounting_message_id": None,
        }
    elif recovery_id and str(recovery.get("recovery_id") or "") not in ("", str(recovery_id)):
        return await _finish(action_status="failed", decision="conflict",
                             reason="recovery_id_mismatch")

    # ---- 取下一页（完整用户消息，禁止截断）----
    cursor_created_at = recovery.get("cursor_created_at")
    upper_created_at = recovery.get("snapshot_last_created_at")
    page_statement = (
        select(ChatMessage)
        .where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "user",
            ChatMessage.created_at
            <= datetime.fromisoformat(str(upper_created_at)),
        )
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(RECOVERY_PAGE_SIZE + 1)
    )
    if cursor_created_at:
        page_statement = page_statement.where(
            ChatMessage.created_at > datetime.fromisoformat(str(cursor_created_at))
        )
    page_rows = list((await session.execute(page_statement)).scalars().all())
    page_rows = page_rows[:RECOVERY_PAGE_SIZE]

    if not page_rows:
        recovery = {**recovery, "status": "awaiting_confirmation"}
        applied = await _commit(
            session, stored=stored, snapshot=stored.snapshot,
            expected_version=stored.state_version,
            new_version=stored.state_version + 1,
            topic_id=stored.topic_id or context_rules.new_topic_id(),
            event_type="recovery", actor="system",
            source_ref={"action_id": str(action_id), "phase": "scan_done"},
            decisions=[], delta={}, reason="awaiting_confirmation", turn_id="",
            recovery=recovery,
        )
        row = await _finish_action(
            session, action_row, status="succeeded",
            result={"decision": "applied", "reason": "awaiting_confirmation"},
            expected_state_version=applied.version,
        )
        return _action_result_view(
            session_id=session_id, stored=stored, action="recover",
            action_id=action_id, row=row,
        )

    # ---- 预算检查：装不下整页就不发出（保持游标）----
    page_text = "\n\n".join(
        f"[消息 {row.id}] {row.content}" for row in page_rows
    )
    rendered = prompt_tool.render(
        RECOVERY_TASK_ID,
        page=_clip_for_prompt(page_text, limit=200_000),
        state=context_rules.render_state(stored.snapshot),
    )
    decision = budget_tool.decide(
        messages=[
            {"role": "system", "content": rendered.system},
            {"role": "user", "content": rendered.user},
        ],
        tools=None,
        output_reserve=int(cfg.context.extract_max_tokens),
        application_limit=cfg.context.request_token_limit,
        model_window=cfg.context.model_context_window,
        mode="enforce",
        protected=("恢复页用户原文",),
    )
    if not decision.allowed:
        row = await _finish_action(
            session, action_row, status="failed",
            error={"reason": "budget_overflow", "retryable": False},
        )
        view = _action_result_view(
            session_id=session_id, stored=stored, action="recover",
            action_id=action_id, row=row,
        )
        view["reason"] = "budget_overflow"
        return view

    # ---- 账目消息：来源是用户动作，不新增虚假用户原文（§3.5 第 2 条）----
    accounting = ChatMessage(
        session_id=session_id, role="assistant", status="running",
        content="", turn_id=uuid.uuid4(),
    )
    session.add(accounting)
    await session.flush()
    accounting_id = uuid.UUID(str(accounting.id))

    try:
        delta, _result = await caller.call(
            RECOVERY_TASK_ID,
            rendered.system,
            rendered.user,
            parse=_parse_delta,
            context={"recovery_id": str(recovery.get("recovery_id") or "")},
            json_mode=True,
            max_tokens=int(cfg.context.extract_max_tokens),
        )
    except (LLMError, SchemaError, PromptError) as exc:
        await update_message(session, accounting_id, status="failed",
                             error=f"恢复失败：{type(exc).__name__}")
        row = await _finish_action(
            session, action_row, status="failed",
            error={"reason": "recovery_model_failed", "retryable": True},
            accounting_message_id=accounting_id,
            recovery_id=uuid.UUID(str(recovery["recovery_id"]))
            if recovery.get("recovery_id") else None,
        )
        view = _action_result_view(
            session_id=session_id, stored=stored, action="recover",
            action_id=action_id, row=row,
        )
        view["reason"] = "recovery_model_failed"
        return view

    # ---- 候选验证：证据必须落在本页某条真实用户消息里 ----
    page_bodies = {str(row.id): str(row.content) for row in page_rows}
    proposals: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for op in delta.operations:
        evidence = str(op.evidence or "")
        source = next(
            (mid for mid, body in page_bodies.items()
             if context_rules.evidence_in(body, evidence)),
            None,
        )
        if source is None or op.op not in ("add", "replace"):
            rejected.append({"value": op.value, "reason": "evidence_or_op_invalid"})
            continue
        proposals.append({
            "kind": op.kind or "goal",
            "value": op.value,
            "scope": "session" if op.scope == "session" else "topic",
            "scope_id": "" if op.scope == "session" else (stored.topic_id or ""),
            "reason": "recovered_candidate",
            "evidence": evidence,
            "conflicts_with": [str(x) for x in (op.target_fact_id and [op.target_fact_id] or [])],
            "operation": "add" if op.op == "add" else "replace",
            "replaces_fact_ids": [str(op.target_fact_id)] if op.op == "replace" else [],
        })
    for raw in delta.proposals:
        evidence = str(raw.get("evidence") or "")
        source = next(
            (mid for mid, body in page_bodies.items()
             if context_rules.evidence_in(body, evidence)),
            None,
        )
        if source is None:
            rejected.append({"value": str(raw.get("value") or ""),
                             "reason": "evidence_invalid"})
            continue
        proposals.append({**raw, "source_message_ids": [source]})

    if rejected and not proposals:
        # 整页候选都不可验证：不推进游标，记录失败与消费
        await update_message(
            session, accounting_id, status="failed",
            prompt_tokens=int(caller.records[-1].get("prompt_tokens") or 0) if caller.records else 0,
            completion_tokens=int(caller.records[-1].get("completion_tokens") or 0) if caller.records else 0,
            error="恢复候选全部未通过验证",
        )
        row = await _finish_action(
            session, action_row, status="failed",
            error={"reason": "recovery_candidates_invalid", "retryable": True},
            accounting_message_id=accounting_id,
            recovery_id=uuid.UUID(str(recovery["recovery_id"]))
            if recovery.get("recovery_id") else None,
        )
        view = _action_result_view(
            session_id=session_id, stored=stored, action="recover",
            action_id=action_id, row=row,
        )
        view["reason"] = "recovery_candidates_invalid"
        return view

    # ---- 候选全部作为待确认提案落库（确认前不激活）----
    delta_out = ContextDelta.from_dict({"operations": [], "proposals": proposals})
    applied = await apply_delta(
        session, cfg, stored, delta_out,
        message_id=str(page_rows[0].id),
        turn_id=str(accounting.turn_id),
        user_text=page_text,
        mode="enforce",
        verify_source=True,
    )
    if applied.conflict or applied.applied is False and not applied.pending:
        await update_message(session, accounting_id, status="failed",
                             error="恢复候选落库失败")
        row = await _finish_action(
            session, action_row, status="failed",
            error={"reason": applied.reason or "recovery_apply_failed",
                   "retryable": True},
            accounting_message_id=accounting_id,
        )
        view = _action_result_view(
            session_id=session_id, stored=stored, action="recover",
            action_id=action_id, row=row,
        )
        view["reason"] = applied.reason or "recovery_apply_failed"
        return view

    # 给恢复提案打上恢复来源（complete_recovery 时据此检查未确认候选）
    latest = await load(session, session_id)
    assert latest is not None
    snapshot = latest.snapshot
    recovery_id_value = str(recovery.get("recovery_id") or "")
    pending_ids: list[str] = []
    for proposal in snapshot.proposals:
        if str(proposal.reason or "") == "recovered_candidate":
            proposal.source_ref = {
                **proposal.source_ref,
                "recovery_id": recovery_id_value,
            }
            pending_ids.append(proposal.proposal_id)
    scanned = int(recovery.get("scanned_user_messages") or 0) + len(page_rows)
    recovery = {
        **recovery,
        "status": "awaiting_confirmation"
        if scanned >= int(recovery.get("total_user_messages") or 0)
        else "running",
        "cursor_created_at": page_rows[-1].created_at.isoformat(),
        "cursor_message_id": str(page_rows[-1].id),
        "scanned_user_messages": scanned,
        "pending_proposal_ids": pending_ids,
        "accounting_message_id": str(accounting_id),
    }
    await _commit(
        session, stored=latest, snapshot=snapshot,
        expected_version=latest.state_version,
        new_version=latest.state_version + 1,
        topic_id=latest.topic_id or context_rules.new_topic_id(),
        event_type="recovery", actor="system",
        source_ref={"action_id": str(action_id), "phase": "page_scanned"},
        decisions=applied.decisions,
        delta={"page_size": len(page_rows), "proposals": len(proposals),
               "rejected": rejected},
        reason="page_scanned", turn_id=str(accounting.turn_id),
        recovery=recovery,
    )
    await update_message(
        session, accounting_id, status="succeeded",
        content=f"[恢复] 扫描 {len(page_rows)} 条历史消息，新增 {len(proposals)} 条待确认候选",
        prompt_tokens=int(caller.records[-1].get("prompt_tokens") or 0) if caller.records else 0,
        completion_tokens=int(caller.records[-1].get("completion_tokens") or 0) if caller.records else 0,
    )
    final = await load(session, session_id)
    assert final is not None
    row = await _finish_action(
        session, action_row, status="succeeded",
        result={"decision": "applied", "proposals": len(proposals),
                "scanned": scanned, "rejected": len(rejected)},
        expected_state_version=int(expected_state_version),
        accounting_message_id=accounting_id,
        recovery_id=uuid.UUID(recovery_id_value) if recovery_id_value else None,
    )
    view = _action_result_view(
        session_id=session_id, stored=final, action="recover",
        action_id=action_id, row=row,
    )
    view["proposals"] = len(proposals)
    return view


async def _apply_complete_recovery(
    session: AsyncSession,
    stored: StoredContext,
    *,
    acknowledge_partial_history: bool,
) -> context_rules.ActionResult:
    """完成恢复（v2 §3.5 第 8 条）：候选清零、缺口须显式确认，之后代表"用户已核对"。"""
    recovery = dict(stored.recovery or {})
    if str(recovery.get("status") or "") == "complete":
        return context_rules.ActionResult(
            snapshot=stored.snapshot, decision="noop", reason="recovery_complete",
        )
    recovery_id_value = str(recovery.get("recovery_id") or "")
    pending = [
        p for p in stored.snapshot.proposals
        if not recovery_id_value
        or str((p.source_ref or {}).get("recovery_id") or "") == recovery_id_value
        or str(p.reason or "") == "recovered_candidate"
    ]
    if pending:
        raise ConflictError(
            "还有未确认的恢复候选，请先逐条确认或拒绝",
            {"reason": "recovery_proposals_pending", "retryable": False,
             "pending": len(pending)},
        )
    scanned = int(recovery.get("scanned_user_messages") or 0)
    total = int(recovery.get("total_user_messages") or 0)
    has_gap = total == 0 or scanned < total or bool(recovery.get("gaps"))
    if has_gap and not acknowledge_partial_history:
        raise ConflictError(
            "历史存在未扫描缺口，确认已核对当前要求后才能完成恢复",
            {"reason": "acknowledge_required", "retryable": False,
             "scanned_user_messages": scanned, "total_user_messages": total},
        )
    snapshot = stored.snapshot.model_copy(deep=True)
    snapshot.recovery_status = "complete"
    return context_rules.ActionResult(
        snapshot=snapshot,
        decision="applied",
        reason="partial_acknowledged" if has_gap else "complete",
        event_type="complete_recovery",
    )


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
    recovery: dict[str, Any] | None = None,
) -> ApplyResult:
    """条件更新快照 + 写审计（同一事务）；失败不留半个状态。"""
    snapshot.state_version = new_version
    snapshot.topic_id = topic_id
    context_state_body = snapshot.to_dict()
    if recovery is not None:
        context_state_body["recovery"] = recovery
    statement = (
        update(ChatSession)
        .where(ChatSession.id == stored.session_id,
               ChatSession.state_version == int(expected_version))
        .values(
            state_version=int(new_version),
            topic_id=topic_id,
            context_state=context_state_body,
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
    recovery = dict(stored.recovery or {})
    recovery_view = {
        "status": str(recovery.get("status") or "complete"),
        "recovery_id": str(recovery.get("recovery_id") or "") or None,
        "reason": str(recovery.get("reason") or ""),
        "snapshot_last_message_id": str(recovery.get("snapshot_last_message_id") or "") or None,
        "cursor_message_id": str(recovery.get("cursor_message_id") or "") or None,
        "scanned_user_messages": int(recovery.get("scanned_user_messages") or 0),
        "total_user_messages": int(recovery.get("total_user_messages") or 0),
        "pending_proposal_ids": [str(x) for x in (recovery.get("pending_proposal_ids") or [])],
        "accounting_message_id": str(recovery.get("accounting_message_id") or "") or None,
    }
    # recovery_status 从恢复进度推导（E7CTX-05）：只有 recovery.status=complete 才是
    # complete；旧会话未恢复 / 恢复中一律 partial，前端显示"历史状态未完整恢复"。
    recovery_status_view = (
        "complete" if str(recovery.get("status") or "") == "complete" else "partial"
    )
    return {
        "session_id": str(stored.session_id),
        "state_version": int(stored.state_version),
        "topic_id": stored.topic_id,
        "recovery_status": recovery_status_view,
        # v2：返回**全部**状态的事实（含 retracted / superseded），撤回结果可被读回
        "facts": [item.to_dict() for item in stored.snapshot.facts],
        "proposals": [item.to_dict() for item in stored.snapshot.proposals],
        "artifact_heads": heads,
        "effective_mode": cfg.context.effective_mode(str(stored.session_id)),
        "effective_mode_reason": cfg.context.mode_reason(str(stored.session_id)),
        "object_refs": [dict(item) for item in (stored.snapshot.object_refs or [])],
        "recovery": recovery_view,
        "blocking_proposals": blockers,
    }


def state_prompt(stored: StoredContext) -> str:
    """装配给 supervisor 的状态段（E7CTX-02：**全部**适用生效事实，不截断；装不下时由
    完整预算检查明确报 required_context_overflow，不静默丢约束）。"""
    lines = ["[当前状态] 用户明确表达、且已有原文证据的要求："]
    for fact in stored.snapshot.active():
        scope = "整个会话" if fact.scope == "session" else (
            f"当前话题（{fact.scope_id}）" if fact.scope == "topic" else f"产物 {fact.scope_id}"
        )
        lines.append(f"- {fact.kind}：{fact.value}（作用域：{scope}）")
    proposals = stored.snapshot.pending()
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

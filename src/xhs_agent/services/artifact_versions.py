"""文案 / 线索版本用例（S7.25）：认领、基础版本、冲突提交与查询。

用途：让连续改稿有确定的版本链——请求先认领（`pending`）再付费调用，成功后在同一事务内
分配成功版本号并切换对象 head；失败 / 拒绝 / 中断只更新终态，不占号、不回写运行初稿。
输入：`AsyncSession`、对象键、请求键、基础版本、内容与用量。
输出：`ChatArtifactVersion` 行与它的契约视图。

口径（方案 §6.1 / §6.2 / §5.1）：

1. **对象键** = 会话 + run + 热点位置（`draft:<run_id>:<position>` / `clue:...`）；
2. **请求键**包含来源轮次与工具调用，不能只用修改文字去重——否则两次相同措辞的改稿会被
   当成同一次请求；
3. 运行初稿登记为对象首个成功版本（`base_version_id` 为 NULL），**原 `run_hotspots.draft`
   不变**；
4. 提交在短事务内**锁定会话行**（`SELECT ... FOR UPDATE`），校验 `expected_state_version`
   与 head 未被其他请求改变；冲突时不覆盖新 head，已发生的消费保留；
5. 观测 `artifact_version_committed` 在提交后写，旁路失败不影响版本。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.errors import ConflictError
from ..core.observations import record as record_observation
from ..db.models import ChatArtifactVersion, ChatSession
from ..util import now_iso

logger = logging.getLogger("xhs_agent.artifact_versions")

INITIAL_REQUEST_PREFIX = "init:"
TERMINAL_STATUSES = ("succeeded", "failed", "refused", "interrupted")


def normalize_draft_content(content: dict[str, Any] | None) -> dict[str, Any]:
    """把版本内容归一成**完整 Draft**（E7CTX-06）。

    - 顶层已是合法 Draft（`titles` / `body` / `tags`）→ 原样返回（校验后）；
    - 旧包裹 `{"draft": {...}}` 或 `{"revised": {...}}`（二选一）→ 校验后取内层；
    - 两个包裹同时存在且内容不同 → 拒绝（`artifact_version_mismatch`），不任选一个；
    - 无法识别 → 抛 `ConflictError`（`reason=artifact_version_mismatch`）。
      **没有"读取失败就回落运行初稿"的路径**——那正是 E7CTX-06 的缺陷。
    """
    body = dict(content or {})
    if not body:
        raise ConflictError(
            "版本内容缺失，无法读取基础正文",
            {"reason": "artifact_version_mismatch", "retryable": False},
        )
    if _is_draft(body):
        return _validate_draft(body)
    has_draft = isinstance(body.get("draft"), dict)
    has_revised = isinstance(body.get("revised"), dict)
    if has_draft and has_revised:
        raise ConflictError(
            "版本内容同时携带 draft 与 revised 包裹，无法确定正文",
            {"reason": "artifact_version_mismatch", "retryable": False},
        )
    if has_draft:
        return _validate_draft(dict(body["draft"]))
    if has_revised:
        return _validate_draft(dict(body["revised"]))
    raise ConflictError(
        "版本内容结构无法识别（既不是 Draft 也不是已知包裹）",
        {"reason": "artifact_version_mismatch", "retryable": False},
    )


def _is_draft(body: dict[str, Any]) -> bool:
    return isinstance(body.get("body"), str) and bool(str(body.get("body") or "").strip())


def _validate_draft(payload: dict[str, Any]) -> dict[str, Any]:
    from ..schemas import Draft, SchemaError

    try:
        draft = Draft.from_dict(dict(payload))
    except SchemaError as exc:
        raise ConflictError(
            f"版本内容不符合 Draft 结构：{exc}",
            {"reason": "artifact_version_mismatch", "retryable": False},
        ) from exc
    return draft.to_dict()


def object_key(object_type: str, run_id: str, position: int) -> str:
    """对象键：`draft:<run_id>:<position>` / `clue:<run_id>:<position>`。"""
    return f"{object_type}:{run_id}:{int(position)}"


def request_key(
    session_id: str | uuid.UUID,
    object_key_value: str,
    *,
    turn_id: str = "",
    call_id: str = "",
    request: str = "",
) -> str:
    """请求认领键：来源（轮次 + 调用）+ 修改文字，缺一不可。"""
    body = "|".join([
        str(session_id),
        str(object_key_value),
        str(turn_id or ""),
        str(call_id or ""),
        str(request or ""),
    ])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class ClaimResult:
    """一次请求认领的结果。"""

    version_id: uuid.UUID | None
    status: str
    is_new: bool
    row: ChatArtifactVersion | None = None

    @property
    def processing(self) -> bool:
        return self.status == "pending" and not self.is_new


async def claim(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    object_key_value: str,
    object_type: str,
    run_id: str | None,
    position: int | None,
    request_key_value: str,
    source: dict[str, Any] | None = None,
    base_version_id: uuid.UUID | None = None,
    expected_head_version_id: uuid.UUID | None = None,
    expected_state_version: int | None = None,
    source_message_id: str | None = None,
) -> ClaimResult:
    """认领一次生成请求；重复的 `request_key` 返回既有行（`is_new=False`）。

    E7CTX-07：认领时**持久保存**实际基础版本、生成前 head、预期状态版本与来源消息；
    提交时比较当前 head 与生成前 head（不要求基础等于 head）。先提交认领行，再发付费
    调用——进程退出时留 `pending`，由读侧显示"状态不明"并提供受控重试。
    """
    statement = (
        insert(ChatArtifactVersion)
        .values(
            session_id=session_id,
            object_key=object_key_value,
            object_type=object_type,
            run_id=_uuid_or_none(run_id),
            hotspot_position=position,
            request_key=request_key_value,
            status="pending",
            base_version_id=base_version_id,
            expected_head_version_id=expected_head_version_id,
            expected_state_version=expected_state_version,
            source_message_id=_uuid_or_none(source_message_id),
            source=dict(source or {}),
        )
        .on_conflict_do_nothing(constraint="uq_chat_artifact_versions_session_request")
        .returning(ChatArtifactVersion.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        await session.commit()
        row = await _load(session, inserted)
        return ClaimResult(version_id=inserted, status="pending", is_new=True, row=row)
    existing = (
        await session.execute(
            select(ChatArtifactVersion).where(
                ChatArtifactVersion.session_id == session_id,
                ChatArtifactVersion.request_key == request_key_value,
            )
        )
    ).scalar_one_or_none()
    if existing is None:  # pragma: no cover - 唯一约束冲突后必然能读到
        return ClaimResult(version_id=None, status="missing", is_new=False)
    return ClaimResult(
        version_id=existing.id,
        status=str(existing.status),
        is_new=False,
        row=existing,
    )


async def ensure_initial(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    object_key_value: str,
    object_type: str,
    run_id: str | None,
    position: int | None,
    content: dict[str, Any],
    source: dict[str, Any] | None = None,
    prompt_versions: dict[str, int] | None = None,
) -> ChatArtifactVersion:
    """把运行初稿登记为对象首个成功版本（幂等）；不修改 `run_hotspots.draft`。

    E7CTX-07：锁内**重查**一次——两个首次请求竞争时，后到者在锁内读到已登记行直接返回，
    不再撞唯一约束。
    """
    key = f"{INITIAL_REQUEST_PREFIX}{object_key_value}"
    chat = await _lock_session(session, session_id)
    existing = (
        await session.execute(
            select(ChatArtifactVersion).where(
                ChatArtifactVersion.session_id == session_id,
                ChatArtifactVersion.request_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    head = await _head_row(session, session_id, object_key_value)
    version_no = (int(head.version_no or 0) if head else 0) + 1
    row = ChatArtifactVersion(
        session_id=session_id,
        object_key=object_key_value,
        object_type=object_type,
        run_id=_uuid_or_none(run_id),
        hotspot_position=position,
        request_key=key,
        status="succeeded",
        version_no=version_no,
        base_version_id=None,
        content=dict(content),
        source={"actor": "run_initial", **dict(source or {})},
        prompt_versions=dict(prompt_versions or {}),
        usage={},
    )
    session.add(row)
    await session.flush()
    _set_head(chat, object_key_value, row)
    await session.commit()
    return row


async def commit_success(
    session: AsyncSession,
    *,
    claim_row: ChatArtifactVersion,
    content: dict[str, Any],
    prompt_versions: dict[str, int] | None = None,
    usage: dict[str, Any] | None = None,
    expected_state_version: int | None = None,
) -> ChatArtifactVersion:
    """提交一次成功版本：按**认领时固化**的预期提交，不接受模型给的新预期值（§3.4）。

    - 锁会话行并**重新读取**数据库当前状态（不信长期 ORM 缓存）；
    - 比较**当前 head 与生成前 head**（`expected_head_version_id`）：不一致 → 409，
      已发生的消费保留；选定基础是旧版也允许（旧版分支），只拦"生成期间出现的新版本"；
    - 冲突 / 拒绝 / 失败 / 取消不推进成功版本；
    - 回滚前先取出错误详情需要的标量，不访问过期 ORM 属性（E7CTX-07）。
    """
    # §3.4：**重新加载认领行**，不信长期 ORM 缓存——回滚 / 其他提交可能已让它过期，
    # 直接读属性会触发同步 IO（MissingGreenlet）；先在绿色线程内 refresh 一次。
    try:
        await session.refresh(claim_row)
    except Exception:  # noqa: BLE001 - 认领行被删：按冲突处理
        await session.rollback()
        raise ConflictError(
            "认领记录已不存在，无法提交版本",
            {"reason": "claim_missing", "retryable": False},
        ) from None
    session_id = claim_row.session_id
    object_key_value = str(claim_row.object_key)
    chat = await _lock_session(session, session_id)
    fresh_claim = (
        await session.execute(
            select(ChatArtifactVersion).where(ChatArtifactVersion.id == claim_row.id)
        )
    ).scalar_one()
    # 回滚会让 ORM 对象过期：冲突分支要用的标量先取出来
    current_state_version = int(chat.state_version or 0)
    claim_expected_head = fresh_claim.expected_head_version_id
    claim_expected_state = fresh_claim.expected_state_version
    base_id = fresh_claim.base_version_id
    head = await _head_row(session, session_id, object_key_value)
    head_id = head.id if head is not None else None
    head_version_no = int(head.version_no or 0) if head is not None else 0

    expected_state = (
        int(claim_expected_state) if claim_expected_state is not None
        else (int(expected_state_version) if expected_state_version is not None else None)
    )
    if expected_state is not None and current_state_version != expected_state:
        await session.rollback()
        raise ConflictError(
            "生成期间会话状态已变化，新版本未提交为最新",
            {
                "reason": "state_version_conflict",
                "state_version": current_state_version,
                "expected_state_version": expected_state,
                "object_key": object_key_value,
                "retryable": True,
            },
        )
    # E7CTX-07：比较的是"生成前 head"，不是"选定基础"——旧版分支可以成功成为新 head
    if claim_expected_head is not None and head_id != claim_expected_head:
        await session.rollback()
        raise ConflictError(
            "生成期间该对象已产生新版本，请基于最新版本重试",
            {
                "reason": "head_changed_during_generation",
                "object_key": object_key_value,
                "head_version_id": str(head_id) if head_id else None,
                "expected_head_version_id": str(claim_expected_head),
                "base_version_id": str(base_id) if base_id else None,
                "state_version": current_state_version,
                "retryable": True,
            },
        )
    if base_id is None and head_id is not None:
        # 首个版本只能登记一次：已存在 head 时不允许再插一个"无基础"的成功版本
        await session.rollback()
        raise ConflictError(
            "该对象已有版本链，请指定基础版本后再提交",
            {
                "reason": "initial_version_exists",
                "object_key": object_key_value,
                "head_version_id": str(head_id),
                "retryable": True,
            },
        )
    version_no = head_version_no + 1
    claim_row.status = "succeeded"
    claim_row.version_no = version_no
    claim_row.content = dict(content)
    claim_row.prompt_versions = dict(prompt_versions or {})
    claim_row.usage = dict(usage or {})
    claim_row.error = None
    claim_row.updated_at = _now()
    _set_head(chat, object_key_value, claim_row)
    await session.commit()
    await record_observation(
        session,
        event_type="artifact_version_committed",
        session_id=session_id,
        key=f"{claim_row.id}:{version_no}",
        payload={
            "object_key": object_key_value,
            "object_type": str(claim_row.object_type),
            "version_id": str(claim_row.id),
            "version_no": version_no,
            "base_version_id": str(base_id) if base_id else None,
            "expected_head_version_id": str(claim_expected_head)
            if claim_expected_head else None,
            "state_version": current_state_version,
            "prompt_versions": dict(prompt_versions or {}),
            "usage_status": str((usage or {}).get("usage_status") or ""),
        },
        turn_id=str((claim_row.source or {}).get("turn_id") or ""),
        call_id=str((claim_row.source or {}).get("tool_call_id") or ""),
        policy_version="artifact_v2",
    )
    return claim_row


async def finish(
    session: AsyncSession,
    claim_row: ChatArtifactVersion,
    *,
    status: str,
    error: str = "",
    usage: dict[str, Any] | None = None,
) -> ChatArtifactVersion:
    """失败 / 拒绝 / 中断：只更新终态与已知消费，不推进成功版本。"""
    if status not in TERMINAL_STATUSES:  # pragma: no cover - 调用方都是常量
        status = "failed"
    claim_row.status = status
    claim_row.error = error[:500] if error else None
    claim_row.usage = dict(usage or {})
    claim_row.updated_at = _now()
    await session.commit()
    return claim_row


async def head(
    session: AsyncSession, session_id: uuid.UUID, object_key_value: str
) -> ChatArtifactVersion | None:
    """对象最近成功版本（没有就返回 None）。"""
    return await _head_row(session, session_id, object_key_value)


async def list_versions(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    object_key_value: str | None = None,
    limit: int = 100,
) -> list[ChatArtifactVersion]:
    """按对象列版本；不传对象键时返回每个对象的最新成功版本（会话内）。"""
    if object_key_value:
        statement = (
            select(ChatArtifactVersion)
            .where(
                ChatArtifactVersion.session_id == session_id,
                ChatArtifactVersion.object_key == object_key_value,
            )
            .order_by(ChatArtifactVersion.created_at.desc())
            .limit(limit)
        )
        return list((await session.execute(statement)).scalars().all())
    rows = list(
        (
            await session.execute(
                select(ChatArtifactVersion)
                .where(
                    ChatArtifactVersion.session_id == session_id,
                    ChatArtifactVersion.status == "succeeded",
                )
                .order_by(ChatArtifactVersion.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, ChatArtifactVersion] = {}
    for row in rows:
        latest.setdefault(str(row.object_key), row)
    return list(latest.values())[:limit]


async def get_version(
    session: AsyncSession, session_id: uuid.UUID, version_id: uuid.UUID
) -> ChatArtifactVersion | None:
    """取指定版本；**只在本会话内**查（跨会话一律当作不存在）。"""
    return (
        await session.execute(
            select(ChatArtifactVersion).where(
                ChatArtifactVersion.id == version_id,
                ChatArtifactVersion.session_id == session_id,
            )
        )
    ).scalar_one_or_none()


def view(row: ChatArtifactVersion) -> dict[str, Any]:
    """ORM 行 → 契约 `ArtifactVersion`。"""
    return {
        "version_id": str(row.id),
        "session_id": str(row.session_id),
        "object_key": str(row.object_key),
        "object_type": str(row.object_type),
        "run_id": str(row.run_id) if row.run_id else None,
        "hotspot_position": int(row.hotspot_position) if row.hotspot_position else None,
        "status": str(row.status),
        "version_no": int(row.version_no) if row.version_no else None,
        "base_version_id": str(row.base_version_id) if row.base_version_id else None,
        "content": dict(row.content) if isinstance(row.content, dict) else None,
        "source": dict(row.source or {}),
        "prompt_versions": {str(k): int(v) for k, v in (row.prompt_versions or {}).items()},
        "usage": dict(row.usage or {}),
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def head_view(
    object_key_value: str, row: ChatArtifactVersion
) -> dict[str, Any]:
    return {
        "object_key": object_key_value,
        "version_id": str(row.id),
        "version_no": int(row.version_no or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    """宽松解析 uuid：非法值按"未关联运行"处理（不让模型参数把整次改稿炸掉）。"""
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def _lock_session(session: AsyncSession, session_id: uuid.UUID) -> ChatSession:
    """锁会话行：同一会话的版本提交串行化（短事务，不做长等待）。"""
    chat = (
        await session.execute(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if chat is None:
        raise ConflictError("会话不存在或已删除", {"session_id": str(session_id)})
    return chat


def _set_head(chat: ChatSession, object_key_value: str, row: ChatArtifactVersion) -> None:
    heads = dict(chat.artifact_heads or {})
    heads[object_key_value] = {
        "version_id": str(row.id),
        "version_no": int(row.version_no or 0),
        "updated_at": now_iso(),
    }
    chat.artifact_heads = heads


async def _head_row(
    session: AsyncSession, session_id: uuid.UUID, object_key_value: str
) -> ChatArtifactVersion | None:
    return (
        await session.execute(
            select(ChatArtifactVersion)
            .where(
                ChatArtifactVersion.session_id == session_id,
                ChatArtifactVersion.object_key == object_key_value,
                ChatArtifactVersion.status == "succeeded",
            )
            .order_by(ChatArtifactVersion.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _load(
    session: AsyncSession, version_id: uuid.UUID
) -> ChatArtifactVersion | None:
    return (
        await session.execute(
            select(ChatArtifactVersion).where(ChatArtifactVersion.id == version_id)
        )
    ).scalar_one_or_none()

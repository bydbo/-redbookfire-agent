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
) -> ClaimResult:
    """认领一次生成请求；重复的 `request_key` 返回既有行（`is_new=False`）。

    先提交认领行，再发付费调用——进程退出时留 `pending`，由读侧显示"状态不明"并提供受控重试。
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
    run_id: str,
    position: int,
    content: dict[str, Any],
    source: dict[str, Any] | None = None,
    prompt_versions: dict[str, int] | None = None,
) -> ChatArtifactVersion:
    """把运行初稿登记为对象首个成功版本（幂等）；不修改 `run_hotspots.draft`。"""
    key = f"{INITIAL_REQUEST_PREFIX}{object_key_value}"
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
    chat = await _lock_session(session, session_id)
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
    """提交一次成功版本（锁会话行 + 校验 head + 分配版本号 + 切 head，同一事务）。

    冲突时抛 `ConflictError`（带当前 head 与状态版本），**不覆盖**其他请求刚完成的新 head。
    """
    session_id = claim_row.session_id
    object_key_value = str(claim_row.object_key)
    chat = await _lock_session(session, session_id)
    # rollback 会让 ORM 对象过期：冲突分支要用的数字先取出来（否则读属性会触发绿色线程外 IO）
    current_state_version = int(chat.state_version or 0)
    if expected_state_version is not None and current_state_version != int(
        expected_state_version
    ):
        await session.rollback()
        raise ConflictError(
            "生成期间会话状态已变化，新版本未提交为最新",
            {
                "state_version": current_state_version,
                "expected_state_version": int(expected_state_version),
                "object_key": object_key_value,
                "retryable": True,
            },
        )
    head = await _head_row(session, session_id, object_key_value)
    base_id = claim_row.base_version_id
    if base_id is not None and (head is None or head.id != base_id):
        await session.rollback()
        raise ConflictError(
            "生成期间该对象已产生新版本，请基于最新版本重试",
            {
                "object_key": object_key_value,
                "head_version_id": str(head.id) if head else None,
                "base_version_id": str(base_id),
                "state_version": current_state_version,
                "retryable": True,
            },
        )
    if base_id is None and head is not None:
        # 首个版本只能登记一次：已存在 head 时不允许再插一个"无基础"的成功版本
        await session.rollback()
        raise ConflictError(
            "该对象已有版本链，请指定基础版本后再提交",
            {
                "object_key": object_key_value,
                "head_version_id": str(head.id),
                "retryable": True,
            },
        )
    version_no = (int(head.version_no or 0) if head else 0) + 1
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
            "state_version": current_state_version,
            "prompt_versions": dict(prompt_versions or {}),
            "usage_status": str((usage or {}).get("usage_status") or ""),
        },
        turn_id=str((claim_row.source or {}).get("turn_id") or ""),
        call_id=str((claim_row.source or {}).get("tool_call_id") or ""),
        policy_version="artifact_v1",
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

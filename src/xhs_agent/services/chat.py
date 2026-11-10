"""对话的会话与消息存储（S7.2）。

用途：建会话、追加/更新消息、按会话读历史，以及把用户传的图片附件落到磁盘。
输入：`AsyncSession`、`AppConfig`（读 `[paths].runs_dir` 与 `[chat]`）。
输出：契约形状的 dict——`ChatSession` / `ChatSessionSummary` / `ChatMessage`
      （落库口径见 `docs/contracts/数据契约.md` §3.6 / §3.7）。

口径：

- 会话标题取**首条用户消息**去空白后的前 20 字（空则「新对话」），可由 PATCH 重命名（S7.30）；
- 附件落 `<runs_dir>/_chat/<session_id>/<uuid>.<ext>`，消息里只存引用
  （`path` 相对工程根 + 正斜杠，与关键帧同一惯例）；magic bytes 与体积校验由接口层做
  （HTTP 语义留在 HTTP 层，与 `POST /api/hotspots/image-clue` 同一分工）；
- 读消息时给每个附件补 `url`（`/api/chat/sessions/{id}/attachments/{message_id}/{index}`），
  前端可以直接当 `<img src>` 用；
- 跨轮图片回落（S7.10）：用户说「上面那张图」时这一轮并没有附件，`latest_attachment` 取本会话
  最近一张图片交给工具（与 `latest_run_id` 同一思路）；S7.16 起 `latest_attachments` 取的是
  **最近一条带图消息的整组**（多图热点一次解析一份线索）；
- `status=running` 的 assistant 行代表"这一轮还没跑完"；进程重启遗留的行由
  `mark_stale_turns` 在读会话时兜底标成 `interrupted`。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from typing import cast as type_cast

from sqlalchemy import CursorResult, DateTime, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PROJECT_ROOT, AppConfig
from ..core import context_rules
from ..core import progress as progress_tool
from ..core.errors import NotFoundError
from ..db.models import ChatMessage, ChatSession

CHAT_DIR_NAME = "_chat"
TITLE_CHARS = 20
DEFAULT_TITLE = "新对话"
PREVIEW_CHARS = 60
# `latest_attachment` 往回找几条消息（一张图可能夹在若干纯文字消息之后）
LOOKBACK_MESSAGES = 20
# 附件 URL 的固定前缀：`api/main.py` 的 API_PREFIX 是契约固定的 "/api"（ADR 0009），
# 这里不 import 它——services 反向依赖 api 会成环。
API_PREFIX = "/api"

IMAGE_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def derive_title(text: str) -> str:
    """会话标题：折成单行后取前 20 字（空消息回落到「新对话」）。"""
    cleaned = " ".join(str(text or "").split())
    return cleaned[:TITLE_CHARS] or DEFAULT_TITLE


def chat_root(cfg: AppConfig) -> str:
    """附件根目录：`<runs_dir>/_chat`（runs/ 已 gitignore 且被 compose 挂载）。"""
    return os.path.join(cfg.runs_dir(), CHAT_DIR_NAME)


def _relative(path: str) -> str:
    """存进库的路径统一成「相对工程根 + 正斜杠」（与关键帧同一惯例）。

    `runs_dir` 与工程根不在同一个盘符时（测试用临时目录就是这个场景），`relpath` 会抛
    `ValueError`——退回绝对路径，保证读接口仍能定位文件（与 `_relative_keyframes` 同一处理）。
    """
    rel = path
    if os.path.isabs(path):
        try:
            rel = os.path.relpath(path, PROJECT_ROOT)
        except ValueError:
            rel = path
    return rel.replace(os.sep, "/")


def attachment_target(cfg: AppConfig, session_id: uuid.UUID | str, mime: str) -> str:
    """给一张附件挑落点：`<runs>/_chat/<session_id>/<uuid>.<ext>`。"""
    suffix = IMAGE_SUFFIX.get(mime, ".bin")
    return os.path.join(chat_root(cfg), str(session_id), f"{uuid.uuid4().hex}{suffix}")


def save_attachment(
    cfg: AppConfig, session_id: uuid.UUID | str, data: bytes, *, name: str, mime: str
) -> dict[str, Any]:
    """把一张图片附件写盘（同步；调用方用 `asyncio.to_thread`），返回消息里要存的引用。

    E7CTX-10（v2）：上传时保存内容身份（sha256）与文件元数据（实际 bytes、mtime_ns）——
    命中前的校验据此判断"外部内容是否被改过"，不能只信旧摘要。
    """
    import hashlib
    from pathlib import Path as _Path

    target = attachment_target(cfg, session_id, mime)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(data)
    stat = _Path(target).stat()
    return {
        "kind": "image",
        "name": os.path.basename(str(name or "image")),
        "mime": mime,
        "bytes": len(data),
        "path": _relative(target),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
    }


def resolve_attachment(cfg: AppConfig, session_id: uuid.UUID | str, path: str) -> str:
    """把消息里的附件路径解析回绝对路径；越出该会话目录一律 404（防穿越）。"""
    root = os.path.realpath(os.path.join(chat_root(cfg), str(session_id)))
    candidate = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
    target = os.path.realpath(candidate)
    if target != root and not target.startswith(root + os.sep):
        raise NotFoundError("附件不存在", {"path": path})
    if not os.path.isfile(target):
        raise NotFoundError("附件不存在", {"path": path})
    return target


def _session_view(row: ChatSession) -> dict[str, Any]:
    return {
        "session_id": str(row.id),
        "title": row.title or DEFAULT_TITLE,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def _attachment_views(
    session_id: str, message_id: str, attachments: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """给附件补 `url`：前端可以直接当 `<img src>`（读接口按 session + message + 下标取）。"""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(attachments or []):
        view = dict(item)
        view["url"] = (
            f"{API_PREFIX}/chat/sessions/{session_id}/attachments/{message_id}/{index}"
        )
        out.append(view)
    return out


def _progress_view(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """阶段快照投影：只有带 `stage` 与合法 `status` 的快照才对外给；其余折成 `null`（S7.21）。

    旧行是 `{}`、崩溃兜底可能写入半成品——读侧统一兜住，避免响应模型因坏快照直接 500。
    """
    data = dict(raw or {})
    status = str(data.get("status") or "")
    if not str(data.get("stage") or "") or status not in progress_tool.STAGE_STATUSES:
        return None
    return data


def message_view(row: ChatMessage) -> dict[str, Any]:
    """ORM 行 → 契约 `ChatMessage` 形状（附件补 url，时间转 ISO）。"""
    session_id = str(row.session_id)
    message_id = str(row.id)
    return {
        "message_id": message_id,
        "session_id": session_id,
        "role": row.role,
        "content": row.content or "",
        "status": row.status,
        "attachments": _attachment_views(session_id, message_id, row.attachments),
        "tool_calls": [dict(item) for item in (row.tool_calls or [])],
        "prompt_versions": {
            str(key): int(value) for key, value in (row.prompt_versions or {}).items()
        },
        "run_id": str(row.run_id) if row.run_id else None,
        "turn_id": str(row.turn_id) if row.turn_id else None,
        "trace_id": row.trace_id,
        # 旧行 / 还没写快照时是空对象，对外折成 null（契约允许）；有快照才给 `StageInfo`
        "progress": _progress_view(row.progress),
        "operation_binding": dict(row.operation_binding) if row.operation_binding else None,
        "cost_cny": float(row.cost_cny or 0),
        "latency_ms": int(row.latency_ms or 0),
        "prompt_tokens": int(row.prompt_tokens or 0),
        "completion_tokens": int(row.completion_tokens or 0),
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _preview(text: str) -> str:
    """会话列表里的末条预览：超 60 字截断加省略号（与 `runs` 列表同一口径）。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= PREVIEW_CHARS else value[:PREVIEW_CHARS] + "…"


async def create_session(session: AsyncSession, *, title: str = "") -> dict[str, Any]:
    """建一个会话；标题留空，等首条用户消息落库时自动补（见 `append_message`）。

    A01：创建事务里写入**明确的维护起点与 `recovery.status=complete`**——新空会话首轮
    不要求用户先恢复；之后普通状态提交默认继承这份恢复结构（见 `context_state._commit`）。
    """
    now = datetime.now(UTC)
    row = ChatSession(
        title=str(title or ""),
        context_state={
            "facts": [],
            "proposals": [],
            "object_refs": [],
            "archived": [],
            "recovery": context_rules.new_recovery(
                baseline_created_at=now.isoformat(), reason="new_session"
            ),
        },
    )
    session.add(row)
    await session.commit()
    return _session_view(row)


async def latest_run_id(session: AsyncSession, session_id: uuid.UUID) -> str:
    """本会话**最近一次**分析产出的 run_id（没有就返回空串）。

    存在的理由（S7.9 冒烟暴露）：`run_id` 只出现在工具的返回值里，而装配给模型的历史只有
    消息**正文**——所以用户在下一轮说「把刚才那版改得更口语一点」时，模型手上根本没有 id。
    让 `read_run` / `revise_draft` 的 `run_id` 可省（缺省就落到这里），多轮对话才真的可用。
    """
    row = (
        await session.execute(
            select(ChatMessage.run_id)
            .where(
                ChatMessage.session_id == session_id, ChatMessage.run_id.is_not(None)
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(row) if row else ""


async def latest_attachments(
    session: AsyncSession, session_id: uuid.UUID
) -> list[dict[str, Any]]:
    """本会话**最近一条带图消息的整组**图片（跨轮图片回落用，S7.10 起、S7.16 改成整组）。

    用户说「上面那几张图」时，这一轮并没有附件、模型也看不到早前的图片字节——由服务端兜底：
    从最近的消息往回找第一条带图的消息（最多看 `LOOKBACK_MESSAGES` 条），把**那一整组**交给
    工具去解析（多图一次调用出一份线索）。没有就返回空列表（工具按"这一轮没有图片"处理）。
    """
    rows = (
        (
            await session.execute(
                select(ChatMessage.attachments)
                .where(
                    ChatMessage.session_id == session_id,
                    func.jsonb_array_length(ChatMessage.attachments) > 0,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(LOOKBACK_MESSAGES)
            )
        )
        .scalars()
        .all()
    )
    for attachments in rows:
        group = [
            dict(item)
            for item in (attachments or [])
            if str(item.get("kind") or "") == "image"
        ]
        if group:
            return group
    return []


async def list_sessions(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """会话列表：`updated_at` 倒序（平局按 id 倒序），页内批量补消息数与末条预览。"""
    total = int(
        (
            await session.execute(select(func.count()).select_from(ChatSession))
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(ChatSession)
                .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    ids = [row.id for row in rows]

    counts: dict[uuid.UUID, int] = {}
    previews: dict[uuid.UUID, str] = {}
    if ids:
        counts = {
            session_id: int(count)
            for session_id, count in (
                await session.execute(
                    select(ChatMessage.session_id, func.count())
                    .where(ChatMessage.session_id.in_(ids))
                    .group_by(ChatMessage.session_id)
                )
            ).all()
        }
        # DISTINCT ON：每个会话只取最后一条消息的正文（Postgres 专属，项目只跑 Postgres）
        latest = (
            select(ChatMessage.session_id, ChatMessage.content)
            .where(ChatMessage.session_id.in_(ids))
            .distinct(ChatMessage.session_id)
            .order_by(
                ChatMessage.session_id,
                ChatMessage.created_at.desc(),
                ChatMessage.id.desc(),
            )
        )
        previews = {
            session_id: _preview(content)
            for session_id, content in (await session.execute(latest)).all()
        }

    items = []
    for row in rows:
        view = _session_view(row)
        view["message_count"] = counts.get(row.id, 0)
        view["last_message_preview"] = previews.get(row.id, "")
        items.append(view)
    return {"items": items, "total": total}


async def load_session(
    session: AsyncSession, session_id: uuid.UUID, *, limit: int | None = None
) -> dict[str, Any] | None:
    """会话详情（含消息）；`limit` 给最近 N 条（内部按时间倒序取再翻回正序）。"""
    row = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    statement = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if limit is None:
        statement = statement.order_by(
            ChatMessage.created_at.asc(), ChatMessage.id.asc()
        )
    else:
        statement = statement.order_by(
            ChatMessage.created_at.desc(), ChatMessage.id.desc()
        ).limit(limit)
    messages = list((await session.execute(statement)).scalars().all())
    if limit is not None:
        messages.reverse()
    return {**_session_view(row), "messages": [message_view(item) for item in messages]}


async def load_message(
    session: AsyncSession, message_id: uuid.UUID
) -> dict[str, Any] | None:
    """单条消息（附件读接口用它找 session_id 与下标）。"""
    row = (
        await session.execute(select(ChatMessage).where(ChatMessage.id == message_id))
    ).scalar_one_or_none()
    return None if row is None else message_view(row)


async def append_message(
    session: AsyncSession,
    session_id: uuid.UUID,
    *,
    role: str,
    content: str = "",
    status: str = "succeeded",
    attachments: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_versions: dict[str, int] | None = None,
    run_id: str | None = None,
    cost_cny: float = 0.0,
    latency_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    turn_id: str | None = None,
    trace_id: str | None = None,
    progress: dict[str, Any] | None = None,
    operation_binding: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    """追加一条消息并刷新会话的 `updated_at`；会话不存在返回 None（接口层折 404）。

    首条**用户**消息同时把会话标题补上（`derive_title`）。
    """
    chat = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if chat is None:
        return None
    # A01：旧代码建的会话没有恢复结构——首条用户消息落库时补上维护起点，避免下一轮
    # `load` 依据"有用户消息且无 recovery"把它误判成未恢复的旧会话。
    needs_recovery = False
    if role == "user" and not isinstance(
        (chat.context_state or {}).get("recovery"), dict
    ):
        prior = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.session_id == session_id,
                           ChatMessage.role == "user")
                )
            ).scalar_one()
        )
        needs_recovery = prior == 0
    row = ChatMessage(
        session_id=session_id,
        role=role,
        content=content or "",
        status=status,
        attachments=list(attachments or []),
        tool_calls=list(tool_calls or []),
        prompt_versions=dict(prompt_versions or {}),
        run_id=uuid.UUID(str(run_id)) if run_id else None,
        turn_id=uuid.UUID(str(turn_id)) if turn_id else None,
        trace_id=str(trace_id) if trace_id else None,
        progress=dict(progress or {}),
        operation_binding=dict(operation_binding) if operation_binding else None,
        cost_cny=Decimal(f"{float(cost_cny or 0):.4f}"),
        latency_ms=int(latency_ms or 0),
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        error=error,
    )
    session.add(row)
    if role == "user" and not (chat.title or "").strip():
        chat.title = derive_title(content)
    if needs_recovery:
        await session.flush()
        state = dict(chat.context_state or {})
        state.setdefault("facts", [])
        state.setdefault("proposals", [])
        state.setdefault("object_refs", [])
        state.setdefault("archived", [])
        state["recovery"] = context_rules.new_recovery(
            baseline_created_at=datetime.now(UTC).isoformat(),
            baseline_message_id=str(row.id),
            reason="new_session",
        )
        chat.context_state = state
    chat.updated_at = datetime.now(UTC)
    await session.commit()
    return message_view(row)


async def update_message(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    content: str | None = None,
    status: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    prompt_versions: dict[str, int] | None = None,
    run_id: str | None = None,
    cost_cny: float | None = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    turn_id: str | None = None,
    trace_id: str | None = None,
    progress: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    """更新一条消息（只改显式给了的字段）；不存在返回 None。"""
    row = (
        await session.execute(select(ChatMessage).where(ChatMessage.id == message_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    if content is not None:
        row.content = content
    if status is not None:
        row.status = status
    if tool_calls is not None:
        row.tool_calls = list(tool_calls)
    if prompt_versions is not None:
        row.prompt_versions = {
            str(key): int(value) for key, value in prompt_versions.items()
        }
    if run_id is not None:
        row.run_id = uuid.UUID(str(run_id))
    if turn_id is not None:
        row.turn_id = uuid.UUID(str(turn_id))
    if trace_id is not None:
        row.trace_id = str(trace_id) or None
    if progress is not None:
        row.progress = dict(progress)
    if cost_cny is not None:
        row.cost_cny = Decimal(f"{float(cost_cny):.4f}")
    if latency_ms is not None:
        row.latency_ms = int(latency_ms)
    if prompt_tokens is not None:
        row.prompt_tokens = int(prompt_tokens)
    if completion_tokens is not None:
        row.completion_tokens = int(completion_tokens)
    if error is not None:
        row.error = error
    await session.commit()
    return message_view(row)


async def _interrupt_stale_row(
    session: AsyncSession, *, message_id: uuid.UUID, cutoff: datetime, detail: str
) -> int:
    """CAS 收尾一行：WHERE 里**再查一次** status 与最近活跃（R5 / E7OBS2-06）。

    SELECT 判定与真正写回之间，主流程可能刚成功 / 刚续约——按旧 ORM 对象直接覆盖会把
    新终态改回 interrupted。这里用一条原子 UPDATE：只有该行**此刻仍 running 且仍陈旧**
    才收敛（受影响 1 行），否则 0 行、不动它；阶段快照用 SQL 局部更新（只在确实 running
    的顶层上改状态 / 类别 / 时间），不整份覆盖。
    """
    statement = text(
        "UPDATE chat_messages SET status = 'interrupted', error = :detail, "
        "progress = CASE WHEN progress->>'status' = 'running' THEN "
        "jsonb_set(jsonb_set(jsonb_set(progress, '{status}', "
        "to_jsonb(CAST(:interrupted AS text))), '{error_category}', "
        "to_jsonb(CAST(:category AS text))), '{updated_at}', "
        "to_jsonb(CAST(:ts AS text))) ELSE progress END "
        "WHERE id = :message_id AND role = 'assistant' AND status = 'running' "
        "AND COALESCE(NULLIF(progress->>'updated_at', '')::timestamptz, created_at) < :cutoff"
    )
    result = type_cast(
        CursorResult[Any],
        await session.execute(
            statement,
            {
                "message_id": message_id,
                "detail": detail,
                "interrupted": "interrupted",
                "category": "timeout",
                "ts": datetime.now(UTC).isoformat(),
                "cutoff": cutoff,
            },
        ),
    )
    return int(result.rowcount or 0)


async def mark_stale_turns(
    session: AsyncSession, *, stale_seconds: int, session_id: uuid.UUID | None = None
) -> int:
    """把"失联太久还挂在 running"的 assistant 行标成 `interrupted`，返回改了几行。

    API 进程重启会带走进程内的 turn（S7.4 的 detached 任务），库里就留下永远 `running` 的行。
    读会话时顺手兜底，前端不再无限等。S7.21：同一行的阶段快照（`progress`）也标成
    `interrupted`——刷新后看到的是"停在哪一阶段"。

    R3（OBS-11）：陈旧判定不再单看 `created_at`——**最近活跃时间** = 快照的 `updated_at`
    （后台任务的心跳租约，见 `chat_turns`），缺省回落 `created_at`（兼容旧快照）。
    R5（E7OBS2-06）：判定只是**候选**，真正写回走 `_interrupt_stale_row` 的 CAS——
    主流程刚成功 / 刚续约的行不会被旧判定覆盖；返回值 = 实际受影响行数，不用旧对象假装。
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=int(stale_seconds))
    last_active = func.coalesce(
        cast(ChatMessage.progress["updated_at"].as_string(), DateTime(timezone=True)),
        ChatMessage.created_at,
    )
    statement = select(ChatMessage.id).where(
        ChatMessage.role == "assistant",
        ChatMessage.status == "running",
        last_active < cutoff,
    )
    if session_id is not None:
        statement = statement.where(ChatMessage.session_id == session_id)
    candidates = list((await session.execute(statement)).scalars().all())
    if not candidates:
        return 0
    detail = "这一轮没跑完就断了（进程重启或长时间无响应）"
    changed = 0
    for message_id in candidates:
        changed += await _interrupt_stale_row(
            session, message_id=message_id, cutoff=cutoff, detail=detail
        )
    await session.commit()
    return changed


async def rename_session(
    session: AsyncSession, session_id: uuid.UUID, title: str
) -> dict[str, Any] | None:
    """只改 `chat_sessions.title`，**不动 `updated_at`**（S7.30）；不存在返回 None。

    请求体已由 `ChatSessionRename` trim / 校验过长度；这里只做一次查行 + 一次 UPDATE + commit，
    不触碰消息、上下文、附件与 `context_state`。`updated_at` 列没有 `onupdate`，只写 `title`
    不会让会话在「按最近活跃倒序」的列表里被顶到最前。

    `_session_view` 刻意在 commit **之前**取值：默认 session factory 可能在 commit 后 expire
    属性，那时再读会隐式多一次 SELECT，与「事务里不做额外读写」的口径冲突。
    """
    row = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    row.title = title
    view = _session_view(row)
    await session.commit()
    return view


async def delete_session(
    session: AsyncSession, cfg: AppConfig, session_id: uuid.UUID
) -> bool:
    """删除会话及其级联数据，并清理**该会话附件根内**的文件（v2 §3.3D）。

    - 会话不存在返回 False（接口层折 404）；
    - 状态、版本、缓存与观测随外键级联删除（数据契约 §3.8–3.11）；
    - 文件清理只处理解析验证过、位于 `<runs>/_chat/<session_id>/` 内的附件；
      单个文件删除失败记录警告，不恢复已删除会话，也不扩大到其他目录。
    """
    row = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        return False
    attachment_rows = list(
        (
            await session.execute(
                select(ChatMessage.attachments).where(
                    ChatMessage.session_id == session_id
                )
            )
        ).scalars().all()
    )
    targets: list[str] = []
    for attachments in attachment_rows:
        for item in attachments or []:
            path = str((item or {}).get("path") or "")
            if not path:
                continue
            try:
                targets.append(resolve_attachment(cfg, session_id, path))
            except NotFoundError:
                continue   # 已缺失的附件不需要清理
    await session.delete(row)
    await session.commit()
    root = os.path.realpath(chat_root(cfg) + os.sep + str(session_id))
    for target in targets:
        resolved = os.path.realpath(target)
        if resolved != root and not resolved.startswith(root + os.sep):
            continue   # 防御：只清理本会话目录内的文件
        try:
            os.remove(resolved)
        except OSError as exc:
            logging.getLogger("xhs_agent.chat").warning(
                "会话附件清理失败（不恢复会话）：%s %s", resolved, exc
            )
    return True

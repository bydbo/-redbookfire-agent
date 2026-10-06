"""对话的会话与消息存储（S7.2）。

用途：建会话、追加/更新消息、按会话读历史，以及把用户传的图片附件落到磁盘。
输入：`AsyncSession`、`AppConfig`（读 `[paths].runs_dir` 与 `[chat]`）。
输出：契约形状的 dict——`ChatSession` / `ChatSessionSummary` / `ChatMessage`
      （落库口径见 `docs/contracts/数据契约.md` §3.6 / §3.7）。

口径：

- 会话标题取**首条用户消息**去空白后的前 20 字（空则「新对话」），不可编辑；
- 附件落 `<runs_dir>/_chat/<session_id>/<uuid>.<ext>`，消息里只存引用
  （`path` 相对工程根 + 正斜杠，与关键帧同一惯例）；magic bytes 与体积校验由接口层做
  （HTTP 语义留在 HTTP 层，与 `POST /api/hotspots/image-clue` 同一分工）；
- 读消息时给每个附件补 `url`（`/api/chat/sessions/{id}/attachments/{message_id}/{index}`），
  前端可以直接当 `<img src>` 用；
- `status=running` 的 assistant 行代表"这一轮还没跑完"；进程重启遗留的行由
  `mark_stale_turns` 在读会话时兜底标成 `interrupted`。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import PROJECT_ROOT, AppConfig
from ..core.errors import NotFoundError
from ..db.models import ChatMessage, ChatSession

CHAT_DIR_NAME = "_chat"
TITLE_CHARS = 20
DEFAULT_TITLE = "新对话"
PREVIEW_CHARS = 60
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


def attachment_target(cfg: AppConfig, session_id: uuid.UUID | str,
                      mime: str) -> str:
    """给一张附件挑落点：`<runs>/_chat/<session_id>/<uuid>.<ext>`。"""
    suffix = IMAGE_SUFFIX.get(mime, ".bin")
    return os.path.join(chat_root(cfg), str(session_id), f"{uuid.uuid4().hex}{suffix}")


def save_attachment(cfg: AppConfig, session_id: uuid.UUID | str, data: bytes, *,
                    name: str, mime: str) -> dict[str, Any]:
    """把一张图片附件写盘（同步；调用方用 `asyncio.to_thread`），返回消息里要存的引用。"""
    target = attachment_target(cfg, session_id, mime)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(data)
    return {"kind": "image", "name": os.path.basename(str(name or "image")), "mime": mime,
            "bytes": len(data), "path": _relative(target)}


def resolve_attachment(cfg: AppConfig, session_id: uuid.UUID | str,
                       path: str) -> str:
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


def _attachment_views(session_id: str, message_id: str,
                      attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """给附件补 `url`：前端可以直接当 `<img src>`（读接口按 session + message + 下标取）。"""
    out: list[dict[str, Any]] = []
    for index, item in enumerate(attachments or []):
        view = dict(item)
        view["url"] = (f"{API_PREFIX}/chat/sessions/{session_id}"
                       f"/attachments/{message_id}/{index}")
        out.append(view)
    return out


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
        "prompt_versions": {str(key): int(value)
                            for key, value in (row.prompt_versions or {}).items()},
        "run_id": str(row.run_id) if row.run_id else None,
        "cost_cny": float(row.cost_cny or 0),
        "latency_ms": int(row.latency_ms or 0),
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _preview(text: str) -> str:
    """会话列表里的末条预览：超 60 字截断加省略号（与 `runs` 列表同一口径）。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= PREVIEW_CHARS else value[:PREVIEW_CHARS] + "…"


async def create_session(session: AsyncSession, *, title: str = "") -> dict[str, Any]:
    """建一个会话；标题留空，等首条用户消息落库时自动补（见 `append_message`）。"""
    row = ChatSession(title=str(title or ""))
    session.add(row)
    await session.commit()
    return _session_view(row)


async def list_sessions(session: AsyncSession, *, limit: int = 50,
                        offset: int = 0) -> dict[str, Any]:
    """会话列表：`updated_at` 倒序（平局按 id 倒序），页内批量补消息数与末条预览。"""
    total = int((await session.execute(
        select(func.count()).select_from(ChatSession))).scalar_one())
    rows = (await session.execute(
        select(ChatSession).order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .limit(limit).offset(offset))).scalars().all()
    ids = [row.id for row in rows]

    counts: dict[uuid.UUID, int] = {}
    previews: dict[uuid.UUID, str] = {}
    if ids:
        counts = {session_id: int(count) for session_id, count in (await session.execute(
            select(ChatMessage.session_id, func.count())
            .where(ChatMessage.session_id.in_(ids))
            .group_by(ChatMessage.session_id))).all()}
        # DISTINCT ON：每个会话只取最后一条消息的正文（Postgres 专属，项目只跑 Postgres）
        latest = (select(ChatMessage.session_id, ChatMessage.content)
                  .where(ChatMessage.session_id.in_(ids))
                  .distinct(ChatMessage.session_id)
                  .order_by(ChatMessage.session_id, ChatMessage.created_at.desc(),
                            ChatMessage.id.desc()))
        previews = {session_id: _preview(content)
                    for session_id, content in (await session.execute(latest)).all()}

    items = []
    for row in rows:
        view = _session_view(row)
        view["message_count"] = counts.get(row.id, 0)
        view["last_message_preview"] = previews.get(row.id, "")
        items.append(view)
    return {"items": items, "total": total}


async def load_session(session: AsyncSession, session_id: uuid.UUID, *,
                       limit: int | None = None) -> dict[str, Any] | None:
    """会话详情（含消息）；`limit` 给最近 N 条（内部按时间倒序取再翻回正序）。"""
    row = (await session.execute(
        select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if row is None:
        return None
    statement = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if limit is None:
        statement = statement.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
    else:
        statement = statement.order_by(ChatMessage.created_at.desc(),
                                       ChatMessage.id.desc()).limit(limit)
    messages = list((await session.execute(statement)).scalars().all())
    if limit is not None:
        messages.reverse()
    return {**_session_view(row), "messages": [message_view(item) for item in messages]}


async def load_message(session: AsyncSession,
                       message_id: uuid.UUID) -> dict[str, Any] | None:
    """单条消息（附件读接口用它找 session_id 与下标）。"""
    row = (await session.execute(
        select(ChatMessage).where(ChatMessage.id == message_id))).scalar_one_or_none()
    return None if row is None else message_view(row)


async def append_message(session: AsyncSession, session_id: uuid.UUID, *, role: str,
                         content: str = "", status: str = "succeeded",
                         attachments: list[dict[str, Any]] | None = None,
                         tool_calls: list[dict[str, Any]] | None = None,
                         prompt_versions: dict[str, int] | None = None,
                         run_id: str | None = None, cost_cny: float = 0.0,
                         latency_ms: int = 0,
                         error: str | None = None) -> dict[str, Any] | None:
    """追加一条消息并刷新会话的 `updated_at`；会话不存在返回 None（接口层折 404）。

    首条**用户**消息同时把会话标题补上（`derive_title`）。
    """
    chat = (await session.execute(
        select(ChatSession).where(ChatSession.id == session_id))).scalar_one_or_none()
    if chat is None:
        return None
    row = ChatMessage(
        session_id=session_id,
        role=role,
        content=content or "",
        status=status,
        attachments=list(attachments or []),
        tool_calls=list(tool_calls or []),
        prompt_versions=dict(prompt_versions or {}),
        run_id=uuid.UUID(str(run_id)) if run_id else None,
        cost_cny=Decimal(f"{float(cost_cny or 0):.4f}"),
        latency_ms=int(latency_ms or 0),
        error=error,
    )
    session.add(row)
    if role == "user" and not (chat.title or "").strip():
        chat.title = derive_title(content)
    chat.updated_at = datetime.now(UTC)
    await session.commit()
    return message_view(row)


async def update_message(session: AsyncSession, message_id: uuid.UUID, *, content: str | None = None,
                         status: str | None = None,
                         tool_calls: list[dict[str, Any]] | None = None,
                         prompt_versions: dict[str, int] | None = None,
                         run_id: str | None = None, cost_cny: float | None = None,
                         latency_ms: int | None = None,
                         error: str | None = None) -> dict[str, Any] | None:
    """更新一条消息（只改显式给了的字段）；不存在返回 None。"""
    row = (await session.execute(
        select(ChatMessage).where(ChatMessage.id == message_id))).scalar_one_or_none()
    if row is None:
        return None
    if content is not None:
        row.content = content
    if status is not None:
        row.status = status
    if tool_calls is not None:
        row.tool_calls = list(tool_calls)
    if prompt_versions is not None:
        row.prompt_versions = {str(key): int(value)
                               for key, value in prompt_versions.items()}
    if run_id is not None:
        row.run_id = uuid.UUID(str(run_id))
    if cost_cny is not None:
        row.cost_cny = Decimal(f"{float(cost_cny):.4f}")
    if latency_ms is not None:
        row.latency_ms = int(latency_ms)
    if error is not None:
        row.error = error
    await session.commit()
    return message_view(row)


async def mark_stale_turns(session: AsyncSession, *, stale_seconds: int,
                           session_id: uuid.UUID | None = None) -> int:
    """把"跑太久还挂在 running"的 assistant 行标成 `interrupted`，返回改了几行。

    API 进程重启会带走进程内的 turn（S7.4 的 detached 任务），库里就留下永远 `running` 的行。
    读会话时顺手兜底：超过 `[chat].turn_stale_seconds` 的一律标中断，前端不再无限等。
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=int(stale_seconds))
    statement = (update(ChatMessage)
                 .where(ChatMessage.role == "assistant",
                        ChatMessage.status == "running",
                        ChatMessage.created_at < cutoff)
                 .values(status="interrupted",
                         error="这一轮没跑完就断了（进程重启或长时间无响应）"))
    if session_id is not None:
        statement = statement.where(ChatMessage.session_id == session_id)
    result = await session.execute(statement)
    await session.commit()
    # `Session.execute(update(...))` 运行时返回 CursorResult（有 rowcount），类型标注只到 Result
    return int(getattr(result, "rowcount", 0) or 0)

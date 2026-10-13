"""对话接口（openapi tag：对话）：会话、消息与 SSE 流式回复（S7.4）。

- `POST /api/chat/sessions`：建会话（标题留给首条用户消息）；
- `GET /api/chat/sessions`：会话列表（最近活跃倒序 + 消息数 + 末条预览）；
- `GET /api/chat/sessions/{session_id}`：会话详情（含消息；读的时候顺手把过期 `running` 标中断）；
- `POST /api/chat/sessions/{session_id}/messages`：multipart（`text` / `files`）→ `text/event-stream`
  （`files` 可重复，1–9 张，同属一组；S7.14 起由 `file` 改名）；
- `GET /api/chat/sessions/{session_id}/attachments/{message_id}/{index}`：取回附件原图。

两条容易踩的口径：

1. **先订阅再起 turn**：事件走 Redis pub/sub，`TurnEvents.open()` 必须在 `start_turn` 之前完成，
   否则最早的几个事件（含 `turn_started`）会掉；
2. **断连不停这一轮**：SSE 生成器只负责转发，turn 是 detached 任务——浏览器关掉页面，
   分析照常跑完并落库（前端下次进来看到终态消息）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from ...config import AppConfig
from ...core.errors import BadRequestError, NotFoundError
from ...services.chat import (
    append_message,
    create_session,
    list_sessions,
    load_message,
    load_session,
    mark_stale_turns,
    resolve_attachment,
    save_attachment,
)
from ...services.chat_turns import HEARTBEAT_S, TurnEvents, sse_frame, start_turn
from ...tools.vision import sniff_image_mime
from ..deps import ConfigDep, DispatcherDep, SessionDep, request_id_header
from ..models import (
    ChatSession,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionList,
    UuidPath,
    error_response,
)

router = APIRouter(tags=["对话"], dependencies=[Depends(request_id_header)])

_READ_CHUNK_BYTES = 1024 * 1024
# 装配历史时从库里往回读的上限（真裁剪按 `[chat].history_token_budget` 在
# `agents/supervisor.py::assemble_history` 里做；这里读一个够大的窗口即可）
_HISTORY_SCAN_LIMIT = 200


def _uuid(raw: str, *, what: str) -> uuid.UUID:
    """解析路径里的 uuid；非法格式按"资源不存在"处理（契约的响应集合里没有 422）。"""
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise NotFoundError(f"{what} 不存在：{raw}", {"id": raw}) from exc


async def _read_upload(file: UploadFile, *, index: int, max_bytes: int) -> bytes:
    """读一张上传图片：单张体积上限（边读边计，超限立刻 400）与空文件校验。

    文案一律带**第几张**（`index` 从 1 起）——多图时用户才知道是哪一张的问题（S7.14）。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BadRequestError(
                f"第 {index} 张超过 {max_bytes // (1024 * 1024)} MB 上限",
                {"index": index, "limit_bytes": max_bytes})
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise BadRequestError(f"第 {index} 张是空文件", {"index": index})
    return data


async def _save_images(cfg: AppConfig, chat_id: uuid.UUID,
                       uploads: list[UploadFile]) -> list[dict[str, Any]]:
    """把这一批上传逐张校验并落盘（S7.14 多图热点）。

    口径：**数量上限先拦**（整批拒绝、不截断）；再逐张校验单张体积与类型（按 magic bytes）；
    读完一张就累计总大小，超合计上限立刻 400。所有 400 文案都指明是第几张。
    """
    limit_count = cfg.chat.attachment_max_count
    if len(uploads) > limit_count:
        raise BadRequestError(
            f"请一次最多发 {limit_count} 张图片（收到 {len(uploads)} 张）",
            {"count": len(uploads), "limit": limit_count})
    max_bytes = cfg.chat.attachment_max_mb * 1024 * 1024
    total_limit = cfg.chat.attachment_total_max_mb * 1024 * 1024
    saved: list[dict[str, Any]] = []
    total = 0
    for index, upload in enumerate(uploads, start=1):
        data = await _read_upload(upload, index=index, max_bytes=max_bytes)
        total += len(data)
        if total > total_limit:
            raise BadRequestError(
                f"这一批图片合计超过 {cfg.chat.attachment_total_max_mb} MB 上限",
                {"total_bytes": total, "limit_bytes": total_limit})
        mime = sniff_image_mime(data)
        if not mime:
            raise BadRequestError(
                f"第 {index} 张不是 jpg / png / webp 图片（按文件内容判断）",
                {"index": index, "content_type": upload.content_type or ""})
        saved.append(await asyncio.to_thread(
            save_attachment, cfg, chat_id, data,
            name=upload.filename or f"热点{index}.png", mime=mime))
    return saved


def _attachment_note(attachments: list[dict[str, Any]]) -> str:
    """附件说明（进模型消息与落库前的提示）：最多列 3 个名字 + 总张数（S7.14）。"""
    if not attachments:
        return ""
    names = [str(item.get("name") or "") for item in attachments]
    listed = "、".join(name for name in names[:3] if name)
    count = len(attachments)
    tail = f" 等 {count} 张" if count > 3 else ""
    return f"{count} 张图片（{listed}{tail}）"


@router.post("/chat/sessions", status_code=201, response_model=ChatSession,
             responses={400: error_response("请求格式或参数不合法"),
                        422: error_response("字段级校验失败"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def create_chat_session(session: SessionDep,
                              payload: ChatSessionCreate | None = None) -> dict[str, Any]:
    """建一个空会话；标题留空，等首条用户消息落库时自动补（S7.2）。"""
    created = await create_session(session, title=(payload.title if payload else "") or "")
    return created


@router.get("/chat/sessions", response_model=ChatSessionList,
            responses={503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def list_chat_sessions(
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    """会话列表：`updated_at` 倒序，带消息数与末条预览。"""
    return await list_sessions(session, limit=limit, offset=offset)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetail,
            responses={404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def get_chat_session(session_id: UuidPath, cfg: ConfigDep, session: SessionDep,
                           limit: Annotated[int | None, Query(ge=1, le=200)] = None
                           ) -> dict[str, Any]:
    """会话详情（含消息）。读之前先把过期的 `running` 行标成中断，避免前端无限等。"""
    chat_id = _uuid(session_id, what="会话")
    await mark_stale_turns(session, stale_seconds=cfg.chat.turn_stale_seconds,
                           session_id=chat_id)
    detail = await load_session(session, chat_id, limit=limit)
    if detail is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})
    return detail


@router.post("/chat/sessions/{session_id}/messages",
             responses={200: {"description": "SSE 事件流",
                              "content": {"text/event-stream": {"schema": {"type": "string"}}}},
                        400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        422: error_response("字段级校验失败"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def post_chat_message(
        session_id: UuidPath, cfg: ConfigDep, session: SessionDep, dispatcher: DispatcherDep,
        text: Annotated[str | None, Form()] = None,
        # 用裸 `File()`：给列表加 `json_schema_extra={"format": "binary"}` 会让 format 落在
        # **数组外层**（FastAPI 的既有行为），与契约里 `items` 的形状对不上；`list[UploadFile]`
        # 本身带 `contentMediaType`，Swagger UI 照常渲染多文件选择器。
        files: Annotated[list[UploadFile] | None, File()] = None
) -> StreamingResponse:
    """发一条消息：落库 → 起 detached 的对话轮次 → 把事件总线转成 SSE 流。

    图片走 `files`（可重复，1–9 张，S7.14）：数量与体积先整批校验（400 文案指明第几张），
    再逐张落盘；这一组图在整轮里是同一次解析、跨轮回落也按整组。
    """
    chat_id = _uuid(session_id, what="会话")
    body = (text or "").strip()
    uploads = list(files or [])
    if not body and not uploads:
        raise BadRequestError("这条消息既没有文字也没有图片")
    if len(body) > 500:
        raise BadRequestError("热点原文（这一条消息）不能超过 500 字", {"chars": len(body)})

    detail = await load_session(session, chat_id, limit=_HISTORY_SCAN_LIMIT)
    if detail is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})

    attachments = await _save_images(cfg, chat_id, uploads)
    note = _attachment_note(attachments)

    # 原样交给装配器：带 tool_calls（模型据此拿到 run_id）与 attachments（提示"早前传过图"）。
    # 只读本会话的消息 → 跨会话天然不串味（S7.10）。
    history = [{"role": item["role"], "content": item["content"],
                "tool_calls": item.get("tool_calls") or [],
                "attachments": item.get("attachments") or []}
               for item in detail["messages"]]
    await append_message(session, chat_id, role="user", content=body,
                         attachments=attachments)
    assistant = await append_message(session, chat_id, role="assistant", status="running")
    if assistant is None:                        # pragma: no cover - 会话刚查过，理论上不会
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})

    events = TurnEvents(cfg, turn_id=str(uuid.uuid4()))
    await events.open()                          # 先订阅，再起 turn（否则会丢最早的几个事件）
    start_turn(cfg, events, session_id=chat_id,
               message_id=uuid.UUID(assistant["message_id"]), history=history,
               user_text=body, attachment_note=note, attachments=attachments,
               dispatcher=dispatcher)
    return StreamingResponse(sse_stream(events), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


async def sse_stream(events: TurnEvents) -> AsyncIterator[str]:
    """把事件总线转成 SSE 帧；空闲 15 秒发一行注释当心跳；终端事件后收尾。

    生成器被取消（浏览器断开）时只停止转发：detached 的 turn 照常跑完并落库。
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def pump() -> None:
        # 用队列搬运而不是直接 wait_for(async_gen)：对异步生成器做超时取消会把它搞坏
        async for item in events.stream():
            await queue.put(item)
        await queue.put(None)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                yield ": ping\n\n"               # 心跳：防中间代理掐掉空闲连接
                continue
            if item is None:
                return
            event = str(item.get("event") or "")
            raw_payload = item.get("data")
            payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
            yield sse_frame(event, payload)
            if event in ("turn_finished", "error"):
                return
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task
        with suppress(Exception):
            await events.aclose()


@router.get("/chat/sessions/{session_id}/attachments/{message_id}/{index}",
            responses={200: {"description": "附件图片",
                             "content": {media: {"schema": {"type": "string",
                                                            "format": "binary"}}
                                         for media in ("image/jpeg", "image/png",
                                                       "image/webp")}},
                       404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def get_chat_attachment(session_id: UuidPath, message_id: UuidPath, index: int,
                              cfg: ConfigDep, session: SessionDep) -> FileResponse:
    """按会话 + 消息 + 下标取回附件；不属于该会话 / 越界 / 文件缺失一律 404。"""
    chat_id = _uuid(session_id, what="会话")
    message = await load_message(session, _uuid(message_id, what="消息"))
    items = (message or {}).get("attachments") or []
    if message is None or str(message.get("session_id")) != str(chat_id) \
            or index < 0 or index >= len(items):
        raise NotFoundError("附件不存在", {"session_id": session_id, "index": index})
    attachment = items[index]
    target = resolve_attachment(cfg, chat_id, str(attachment.get("path") or ""))
    return FileResponse(target, media_type=str(attachment.get("mime") or "image/jpeg"),
                        filename=str(attachment.get("name") or "") or None)

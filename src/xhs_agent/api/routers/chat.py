"""对话接口（openapi tag：对话）：会话、消息与 SSE 流式回复（S7.4）。

- `POST /api/chat/sessions`：建会话（标题留给首条用户消息）；
- `GET /api/chat/sessions`：会话列表（最近活跃倒序 + 消息数 + 末条预览）；
- `GET /api/chat/sessions/{session_id}`：会话详情（含消息；读的时候顺手把过期 `running` 标中断）；
- `POST /api/chat/sessions/{session_id}/messages`：multipart（`text` / `file`）→ `text/event-stream`；
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


def _uuid(raw: str, *, what: str) -> uuid.UUID:
    """解析路径里的 uuid；非法格式按"资源不存在"处理（契约的响应集合里没有 422）。"""
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise NotFoundError(f"{what} 不存在：{raw}", {"id": raw}) from exc


async def _read_image(file: UploadFile, *, max_bytes: int) -> bytes:
    """读上传图片并做体积上限（边读边计，超限立刻 400）与空文件校验。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BadRequestError(f"图片超过 {max_bytes // (1024 * 1024)} MB 上限",
                                  {"limit_bytes": max_bytes})
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise BadRequestError("图片为空：file 字段没有内容")
    return data


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
        file: Annotated[UploadFile | None,
                        File(json_schema_extra={"format": "binary"})] = None
) -> StreamingResponse:
    """发一条消息：落库 → 起 detached 的对话轮次 → 把事件总线转成 SSE 流。"""
    chat_id = _uuid(session_id, what="会话")
    body = (text or "").strip()
    if not body and file is None:
        raise BadRequestError("这条消息既没有文字也没有图片")
    if len(body) > 500:
        raise BadRequestError("热点原文（这一条消息）不能超过 500 字", {"chars": len(body)})

    detail = await load_session(session, chat_id, limit=cfg.chat.history_max_messages)
    if detail is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})

    attachments: list[dict[str, Any]] = []
    note = ""
    if file is not None:
        raw = await _read_image(file, max_bytes=cfg.chat.attachment_max_mb * 1024 * 1024)
        mime = sniff_image_mime(raw)
        if not mime:
            raise BadRequestError("只支持 jpg / png / webp 图片（按文件内容判断）",
                                  {"content_type": file.content_type or ""})
        saved = await asyncio.to_thread(save_attachment, cfg, chat_id, raw,
                                        name=file.filename or "热点.png", mime=mime)
        attachments.append(saved)
        note = f"1 张图片（{saved['name']}，{saved['mime']}）"

    history = [{"role": item["role"], "content": item["content"]}
               for item in detail["messages"] if item.get("content")]
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

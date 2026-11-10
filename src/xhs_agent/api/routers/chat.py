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

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from ...config import AppConfig
from ...core.errors import BadRequestError, NotFoundError
from ...core.tracing import get_tracer
from ...services import artifact_versions, context_state
from ...services.chat import (
    append_message,
    create_session,
    delete_session,
    list_sessions,
    load_message,
    load_session,
    mark_stale_turns,
    rename_session,
    resolve_attachment,
    save_attachment,
)
from ...services.chat_turns import HEARTBEAT_S, TurnEvents, sse_frame, start_turn
from ...tools.llm import StructuredCaller, build_provider
from ...tools.vision import sniff_image_mime
from ..deps import ConfigDep, DispatcherDep, SessionDep, request_id_header
from ..models import (
    ArtifactVersion,
    ArtifactVersionList,
    ChatContext,
    ChatSession,
    ChatSessionCreate,
    ChatSessionDetail,
    ChatSessionList,
    ChatSessionRename,
    ClueSelectRequest,
    ClueVersionRequest,
    ContextActionRequest,
    ContextActionResponse,
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
    # S7.24：详情里带一份上下文摘要（旧客户端忽略即可）；off 模式不额外读状态
    if cfg.context.mode != "off":
        stored = await context_state.load(session, chat_id)
        if stored is not None:
            await context_state.assess_recovery(session, chat_id)
            stored = await context_state.load(session, chat_id)
            detail["context"] = (
                context_state.context_view(cfg, stored) if stored is not None else None
            )
    # S7.21：有有效 trace 关联且追踪可用时才给回看地址；追踪关闭 / 旧数据下为 None，
    # 但阶段与失败信息照常可见（它们来自数据库，不依赖 Langfuse）。
    tracer = get_tracer(cfg)
    for message in detail["messages"]:
        trace_id = str(message.get("trace_id") or "")
        message["trace_url"] = tracer.trace_url(trace_id) if trace_id else None
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
        # v2 §3.3B：可选的结构化操作绑定（JSON 字符串，服务端严格校验后随用户消息持久保存）
        operation_binding: Annotated[str | None, Form()] = None,
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

    binding = await context_state.validate_operation_binding(
        session, chat_id, operation_binding
    )
    attachments = await _save_images(cfg, chat_id, uploads)
    note = _attachment_note(attachments)

    # 原样交给装配器：带 tool_calls（模型据此拿到 run_id）与 attachments（提示"早前传过图"）。
    # 只读本会话的消息 → 跨会话天然不串味（S7.10）。
    history = [{"role": item["role"], "content": item["content"],
                "tool_calls": item.get("tool_calls") or [],
                "attachments": item.get("attachments") or []}
               for item in detail["messages"]]
    user = await append_message(session, chat_id, role="user", content=body,
                                attachments=attachments, operation_binding=binding)
    assistant = await append_message(session, chat_id, role="assistant", status="running")
    if assistant is None:                        # pragma: no cover - 会话刚查过，理论上不会
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})

    events = TurnEvents(cfg, turn_id=str(uuid.uuid4()))
    await events.open()                          # 先订阅，再起 turn（否则会丢最早的几个事件）
    start_turn(cfg, events, session_id=chat_id,
               message_id=uuid.UUID(assistant["message_id"]), history=history,
               user_text=body, attachment_note=note, attachments=attachments,
               dispatcher=dispatcher,
               # S7.24：本轮用户消息的**真实 ID**（状态证据与来源都落到它上面）
               user_message_id=uuid.UUID(user["message_id"]) if user else None)
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


@router.get("/chat/sessions/{session_id}/context", response_model=ChatContext,
            responses={404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def get_chat_context(session_id: UuidPath, cfg: ConfigDep,
                           session: SessionDep) -> dict[str, Any]:
    """会话上下文（S7.24）：有效 / 待确认状态、版本、来源、恢复状态与产物 head。

    读的时候顺手做一次恢复判定：历史扫描不完整、来源消息已删或证据对不上时标 `partial`，
    前端显示「历史状态未完整恢复」——**不从旧摘要猜回约束**。
    """
    chat_id = _uuid(session_id, what="会话")
    stored = await context_state.load(session, chat_id)
    if stored is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})
    await context_state.assess_recovery(session, chat_id)
    stored = await context_state.load(session, chat_id)
    if stored is None:  # pragma: no cover - 刚读过
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})
    return context_state.context_view(cfg, stored)


@router.post("/chat/sessions/{session_id}/context/actions",
             response_model=ContextActionResponse,
             responses={202: {"model": ContextActionResponse,
                              "description": "恢复页已在处理中：只读取状态等待，不重放付费页"},
                        400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        409: error_response("状态版本冲突（响应带当前版本，可重试）"),
                        422: error_response("字段级校验失败"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def post_chat_context_action(session_id: UuidPath, payload: ContextActionRequest,
                                   cfg: ConfigDep, session: SessionDep,
                                   response: Response) -> dict[str, Any]:
    """上下文用户动作（v2 §3.3A）：confirm / reject / retract / replace / reset_topic /
    select_object / recover / complete_recovery。

    - `action_id` 是幂等键：同 ID 同请求体重放返回原结果，不同请求体 409；
    - `expected_state_version` 是 CAS 条件：不一致返回 409（响应带当前版本，前端刷新后重试）；
    - `recover` 会调用一次恢复模型（按页扫描历史，候选全部待确认）；无授权配置时 400。
    """
    chat_id = _uuid(session_id, what="会话")
    caller: StructuredCaller | None = None
    if payload.action == "recover":
        from ...tools.http import build_http_client

        async with build_http_client(cfg.llm.timeout_s) as client:
            caller = StructuredCaller(
                provider=build_provider(cfg, client=client),
                max_repairs=int(cfg.context.extract_repair_max),
            )
            applied = await context_state.perform_action(
                session, chat_id,
                action=payload.action,
                target_id=payload.target_id or "",
                value=payload.value or "",
                expected_state_version=payload.expected_state_version,
                action_id=payload.action_id,
                object_ref=payload.object_ref,
                recovery_id=payload.recovery_id,
                retry_of=payload.retry_of,
                acknowledge_partial_history=payload.acknowledge_partial_history,
                cfg=cfg,
                caller=caller,
            )
    else:
        applied = await context_state.perform_action(
            session, chat_id,
            action=payload.action,
            target_id=payload.target_id or "",
            value=payload.value or "",
            expected_state_version=payload.expected_state_version,
            action_id=payload.action_id,
            object_ref=payload.object_ref,
            recovery_id=payload.recovery_id,
            retry_of=payload.retry_of,
            acknowledge_partial_history=payload.acknowledge_partial_history,
        )
    stored = await context_state.load(session, chat_id)
    assert stored is not None   # 动作内部已校验会话存在
    decision = str(applied.get("decision") or "noop")
    status = str(applied.get("status") or "succeeded")
    if status == "interrupted" and applied.get("reason") in ("recovery_running",
                                                             "recovery_retry_required"):
        # 已有在飞认领 / 失败页需显式重试：**真实 202** + 当前上下文，不自动重试付费页
        response.status_code = 202
        return {
            **applied,
            "decision": "noop",
            "context": context_state.context_view(cfg, stored),
        }
    _ = decision
    return {
        **applied,
        "context": context_state.context_view(cfg, stored),
    }


@router.patch("/chat/sessions/{session_id}", response_model=ChatSession,
               responses={404: error_response("资源不存在"),
                          422: error_response("字段级校验失败"),
                          503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def rename_chat_session(session_id: UuidPath, payload: ChatSessionRename,
                              session: SessionDep) -> dict[str, Any]:
    """重命名会话（S7.30）：只改 `title`，不改消息 / 上下文 / `updated_at`。"""
    chat_id = _uuid(session_id, what="会话")
    renamed = await rename_session(session, chat_id, payload.title)
    if renamed is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})
    return renamed


@router.delete("/chat/sessions/{session_id}", status_code=204,
               responses={404: error_response("资源不存在"),
                          503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def delete_chat_session(session_id: UuidPath, cfg: ConfigDep,
                              session: SessionDep) -> None:
    """删除会话（v2 §3.3D）：级联状态 / 版本 / 缓存 / 观测，并清理该会话附件根内的文件。"""
    chat_id = _uuid(session_id, what="会话")
    deleted = await delete_session(session, cfg, chat_id)
    if not deleted:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})
    return None


@router.post("/chat/sessions/{session_id}/artifacts/clue-versions",
             response_model=ContextActionResponse,
             responses={201: {"model": ContextActionResponse,
                              "description": "新版本已创建、选定并原子提交"},
                        400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        409: error_response("状态版本冲突"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def create_clue_version(session_id: UuidPath, payload: ClueVersionRequest,
                              cfg: ConfigDep, session: SessionDep,
                              response: Response) -> dict[str, Any]:
    """保存用户编辑的线索版本（v2 §3.3C）：独立不可变业务版本，成功后设为选定引用。"""
    chat_id = _uuid(session_id, what="会话")
    applied = await context_state.edit_clue_version(
        session, chat_id, cfg,
        action_id=payload.action_id,
        expected_state_version=payload.expected_state_version,
        expected_head_version_id=payload.expected_head_version_id,
        object_key=payload.object_key,
        base_version_id=payload.base_version_id,
        content=payload.content,
    )
    stored = await context_state.load(session, chat_id)
    assert stored is not None
    response.status_code = 200 if applied.get("replay") else 201
    return {**applied, "context": context_state.context_view(cfg, stored)}


@router.post("/chat/sessions/{session_id}/artifacts/{version_id}/select",
             response_model=ContextActionResponse,
             responses={400: error_response("请求格式或参数不合法"),
                        404: error_response("资源不存在"),
                        409: error_response("状态版本冲突"),
                        503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def select_clue_version(session_id: UuidPath, version_id: UuidPath,
                              payload: ClueSelectRequest, cfg: ConfigDep,
                              session: SessionDep) -> dict[str, Any]:
    """选定一个线索版本（v2 §3.3C）：只选同会话成功 clue 版本；推进状态版本并写审计。"""
    _ = cfg
    chat_id = _uuid(session_id, what="会话")
    applied = await context_state.select_clue_version(
        session, chat_id,
        action_id=payload.action_id,
        expected_state_version=payload.expected_state_version,
        version_id=_uuid(version_id, what="版本"),
    )
    stored = await context_state.load(session, chat_id)
    assert stored is not None
    return {**applied, "context": context_state.context_view(cfg, stored)}


@router.get("/chat/sessions/{session_id}/artifacts",
            response_model=ArtifactVersionList,
            responses={404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def list_chat_artifacts(session_id: UuidPath, session: SessionDep,
                              object_key: Annotated[str | None, Query()] = None
                              ) -> dict[str, Any]:
    """按对象列出版本（S7.25）；不传对象键时给每个对象的最新成功版本。"""
    chat_id = _uuid(session_id, what="会话")
    detail = await load_session(session, chat_id, limit=1)
    if detail is None:
        raise NotFoundError(f"会话不存在：{session_id}", {"session_id": session_id})
    rows = await artifact_versions.list_versions(
        session, chat_id, object_key_value=object_key or None
    )
    return {"items": [artifact_versions.view(row) for row in rows], "total": len(rows)}


@router.get("/chat/sessions/{session_id}/artifacts/{version_id}",
            response_model=ArtifactVersion,
            responses={404: error_response("资源不存在"),
                       503: error_response("依赖不可用（不做降级，直接返回错误）")})
async def get_chat_artifact(session_id: UuidPath, version_id: UuidPath,
                            session: SessionDep) -> dict[str, Any]:
    """读取指定不可变版本；不属于该会话一律 404。"""
    chat_id = _uuid(session_id, what="会话")
    row = await artifact_versions.get_version(
        session, chat_id, _uuid(version_id, what="版本")
    )
    if row is None:
        raise NotFoundError(f"版本不存在：{version_id}",
                            {"session_id": session_id, "version_id": version_id})
    return artifact_versions.view(row)


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

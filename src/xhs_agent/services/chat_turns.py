"""对话轮次的运行器与事件总线（S7.4）。

用途：把「发一条消息」变成一次真正的对话——写好 `running` 的 assistant 行 → 跑对话图（S7.3）
      → 把结果写回消息 → 全程通过 Redis pub/sub 广播事件，SSE 端点订阅后转发给浏览器。
输入：`AppConfig`、会话与消息、历史、本轮文本与附件、投递器（把分析投给 Celery）。
输出：事件流（`turn_started` / `stage` / `tool_started` / `tool_progress` / `tool_finished` /
      `text_delta` / `turn_finished` / `error`）与落库后的消息。

口径：

- **turn 跑在 detached asyncio 任务里**：浏览器断开只取消"转发"，不取消这一轮——结果照常落库。
  分析本来就在 Celery worker 跑；对话层的模型调用与图片解析接口同一先例（在 API 进程内等待）。
- S7.19 / S7.20：整轮一个 `chat.turn` span，`tool.<name>` 是它的子 span；`turn_id` 绑进日志
  上下文（结束后复位），随 Celery 业务头传给 worker；阶段生命周期走 `stage` 事件。
- 事件走 Redis pub/sub（复用现有 redis 依赖，channel = `xhs_agent:chat:{turn_id}`）；
  **不做事件重放**：刷新后的中间态靠库里 `status=running` 的行 + 轮询补齐（数据契约 §3.7）。
- 订阅（`TurnEvents.open()`）必须在起 turn **之前**完成，否则最早的几个事件会被漏掉。
- 事件是旁路：发布失败只 warning，不影响这一轮落库；模型/工具失败则把消息标 `failed`
  并发一条终端 `error` 事件。
- 进程重启会带走进程内的 turn：库里留下 `running` 的行，读会话时按
  `[chat].turn_stale_seconds` 标 `interrupted`（`services/chat.py::mark_stale_turns`）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core import progress as progress_tool
from ..core.errors import DependencyUnavailableError
from ..core.logging import bind_turn_id, reset_turn_id
from ..core.progress import error_category as error_category_of
from ..core.progress import label_for
from ..core.tracing import (
    LEVEL_ERROR,
    NullTracer,
    Tracer,
    TracingProvider,
    capture_content,
    get_tracer,
)
from ..db.session import create_engine_from_config, create_session_factory
from ..services import chat_tools
from ..services.chat import latest_attachments, update_message
from ..services.chat_tools import ToolContext, ToolOutcome
from ..tools.http import build_http_client
from ..tools.llm import BaseProvider, StructuredCaller, build_provider
from ..util import now_iso
from ..workflows.chat import ChatTurn, run_turn

logger = logging.getLogger("xhs_agent.chat")

CHANNEL_PREFIX = "xhs_agent:chat:"
HEARTBEAT_S = 15.0

# detached 任务必须自己保活，否则可能被 GC 掉（结果会"半路消失"）
_RUNNING: set[asyncio.Task[None]] = set()


def redis_url(cfg: AppConfig) -> str:
    """事件总线用的 REDIS_URL；缺失或前缀不对直接抛（配置契约 §2.1.1：P3 起必填）。"""
    url = (cfg.env_view.get("REDIS_URL") or "").strip()
    if not url.startswith(("redis://", "rediss://")):
        raise DependencyUnavailableError(
            "对话事件总线需要 REDIS_URL（redis:// 或 rediss://）",
            {"hint": "把 REDIS_URL 写进 config/.env 或环境变量（配置契约 §2.1.1）"})
    return url


def sse_frame(event: str, payload: dict[str, Any]) -> str:
    """SSE 帧：`event: <名>` + 一行 JSON 的 data（JSON 转义保证 data 永远只有一行）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _decode(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


class TurnEvents:
    """一个 turn 的事件总线：发布端由运行器持有，订阅端由 SSE 生成器持有。"""

    def __init__(self, cfg: AppConfig, turn_id: str) -> None:
        self.cfg = cfg
        self.turn_id = turn_id
        self.channel = f"{CHANNEL_PREFIX}{turn_id}"
        self._publisher: Any = None
        self._subscriber: Any = None
        self._pubsub: Any = None
        self._queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._pump: asyncio.Task[None] | None = None

    # ---------- 发布端（运行器） ----------

    async def publish(self, event: str, payload: dict[str, Any]) -> None:
        """发一条事件；Redis 不可用只记警告——事件是旁路，不能因此让这一轮失败。"""
        try:
            if self._publisher is None:
                self._publisher = aioredis.from_url(redis_url(self.cfg))
            body = json.dumps({"event": event, "data": payload}, ensure_ascii=False)
            await self._publisher.publish(self.channel, body)
        except Exception:                       # noqa: BLE001 - 旁路组件，绝不阻断主链路
            logger.warning("对话事件发布失败（不影响落库）：%s", event, exc_info=True)

    async def close_publisher(self) -> None:
        if self._publisher is not None:
            with suppress(Exception):
                await self._publisher.aclose()
            self._publisher = None

    # ---------- 订阅端（SSE 生成器） ----------

    async def open(self) -> None:
        """建立订阅并开始把事件搬进内存队列；**必须在起 turn 之前调用**。"""
        if self._queue is not None:
            return
        self._queue = asyncio.Queue()
        self._subscriber = aioredis.from_url(redis_url(self.cfg))
        self._pubsub = self._subscriber.pubsub()
        await self._pubsub.subscribe(self.channel)
        self._pump = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        queue = self._queue
        if queue is None:                        # pragma: no cover - open() 之后必然有队列
            return
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                item = _decode(message.get("data"))
                if item is not None:
                    await queue.put(item)
        except Exception:                        # noqa: BLE001 - 订阅断了就让转发侧收到 None
            logger.warning("对话事件订阅中断：%s", self.turn_id, exc_info=True)
        finally:
            await queue.put(None)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """逐条产出事件（直到队列收到结束标记）；心跳由 SSE 生成器加。"""
        if self._queue is None:                  # pragma: no cover - 调用方都会先 open()
            return
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if self._pubsub is not None:
            with suppress(Exception):
                await self._pubsub.unsubscribe(self.channel)
                await self._pubsub.aclose()
            self._pubsub = None
        if self._subscriber is not None:
            with suppress(Exception):
                await self._subscriber.aclose()
            self._subscriber = None
        self._queue = None


# ---------- 可注入点（测试替换这两处，生产走真实实现） ----------

def build_turn_provider(cfg: AppConfig, client: Any, *,
                        tracer: Tracer | None = None) -> BaseProvider:
    """对话层用的模型 provider：默认套一层追踪（S7.19）。

    与分析链路同一口径（`workflows/analysis.py`）：追松开时每次 supervisor 调用一个
    generation，`complete_with_tools` 的流式增量原样透传；三件套缺失 / 上报失败都不影响
    这一轮（`TracingProvider` 内部吞掉异常）。`cast` 只是给 mypy 一个交代——`TracingProvider`
    是鸭子类型，`core/` 不反向依赖 `tools/`。
    """
    inner = build_provider(cfg, client=client)
    return cast(BaseProvider, TracingProvider(
        inner, tracer if tracer is not None else get_tracer(cfg),
        capture=capture_content(cfg),
        price_in_per_m=cfg.llm.price_in_per_m,
        price_out_per_m=cfg.llm.price_out_per_m))


def _enqueue_with_turn(enqueue: Any, turn_id: str) -> Any:
    """把 `turn_id` 一并传给支持它的投递器（S7.20）。

    只收 `job_id` 的旧替身（离线测试）原样返回——投递语义不变，只是不带业务标识；
    真实 `CeleryDispatcher.enqueue` 能收 `turn_id`，worker 侧据此绑定日志上下文。
    """
    if enqueue is None or not turn_id:
        return enqueue
    try:
        supports = "turn_id" in inspect.signature(enqueue).parameters
    except (TypeError, ValueError):              # 内建 / C 实现没有可查签名
        supports = False
    if not supports:
        return enqueue

    async def wrapped(job_id: str) -> None:
        await enqueue(job_id, turn_id=turn_id)

    return wrapped


def build_tool_runner(session: AsyncSession, *, cfg: AppConfig, client: Any,
                      session_id: uuid.UUID, attachments: list[dict[str, Any]],
                      dispatcher: Any, caller: Any = None,
                      recent_attachments: list[dict[str, Any]] | None = None,
                      turn_id: str = "", tracer: Tracer | None = None) -> Any:
    """工具执行器：按 `chat_tools.HANDLERS` 分派（S7.7a）——这里也不写具体工具名。

    `caller` 是给只读工具用的文本模型入口（拆解热点等）；`cache` 让同一轮内的同一件事
    只付一次钱（S7.7）。`recent_attachments` 是本会话最近**一组**图（S7.10 的跨轮图片回落，
    S7.16 改成整组）：这一轮没有附件时，用户说「上面那几张图」的工具调用才有东西可用。
    S7.20：`turn_id` 随分析投递传给 worker（绑日志上下文）；`tracer` 给每次工具调用开一个
    `tool.<name>` span——提交分析时的 traceparent 就取自它（worker 的父 span 口径）。
    """
    active = tracer if tracer is not None else NullTracer()
    context = ToolContext(cfg=cfg, session=session, session_id=session_id,
                          enqueue=_enqueue_with_turn(getattr(dispatcher, "enqueue", None), turn_id),
                          http=client, caller=caller,
                          recent_attachments=list(recent_attachments or []))

    async def runner(name: str, args: dict[str, Any], *, on_progress: Any = None) -> Any:
        handler = chat_tools.HANDLERS.get(name)
        if handler is None:                      # 图已按清单过滤过；这里只是兜底，不炸整轮
            return ToolOutcome(status="failed", error=f"未知工具：{name}")
        started = time.monotonic()
        with active.tool(name=f"tool.{name}",
                         metadata={"tool": name, "turn_id": turn_id}) as fields:
            try:
                outcome = await handler(context, args, attachments=attachments,
                                        on_progress=on_progress)
            except BaseException as exc:
                fields.level = LEVEL_ERROR
                fields.status_message = error_category_of(exc)
                raise
            fields.metadata["status"] = outcome.status
            fields.metadata["latency_ms"] = int((time.monotonic() - started) * 1000)
            if outcome.status != "succeeded":
                # 只记安全类别：错误正文可能含用户原文 / 路径，不进追踪元数据（约定 §9）
                fields.level = LEVEL_ERROR
                fields.status_message = "tool_failed"
            return outcome

    return runner


# ---------- 运行器 ----------

def start_turn(cfg: AppConfig, events: TurnEvents, *, session_id: uuid.UUID,
               message_id: uuid.UUID, history: list[dict[str, Any]], user_text: str,
               attachment_note: str = "", attachments: list[dict[str, Any]] | None = None,
               dispatcher: Any = None) -> asyncio.Task[None]:
    """起一个 detached 任务跑完这一轮（调用方不 await；SSE 只订阅事件）。"""
    task = asyncio.create_task(run_turn_task(
        cfg, events, session_id=session_id, message_id=message_id, history=history,
        user_text=user_text, attachment_note=attachment_note, attachments=attachments or [],
        dispatcher=dispatcher))
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


async def run_turn_task(cfg: AppConfig, events: TurnEvents, *, session_id: uuid.UUID,
                        message_id: uuid.UUID, history: list[dict[str, Any]], user_text: str,
                        attachment_note: str = "", attachments: list[dict[str, Any]] | None = None,
                        dispatcher: Any = None) -> None:
    """任务体：自建 engine / httpx，跑完这一轮并把结果写回消息、广播事件。

    S7.19 / S7.20：整轮包在 `chat.turn` span 里（detached 任务继承到请求上下文就挂其下，
    不继承也能自成因）；`turn_id` 绑进日志上下文并在 `finally` 复位；结束时按真实终态
    收尾 span（成功 / 失败都有终态，不永远挂在 running）。
    S7.21：`turn_id` 尽早落库，阶段变化合并进 `chat_messages.progress`——**只在切换时写**，
    不逐 token、不逐轮询；收尾写 `trace_id`，刷新后仍能定位阶段与失败原因。
    """
    engine = create_engine_from_config(cfg)
    token = bind_turn_id(events.turn_id)
    tracer = get_tracer(cfg)
    snapshot = progress_tool.normalize({})
    written = ""
    try:
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                await events.publish("turn_started", {
                    "turn_id": events.turn_id, "session_id": str(session_id),
                    "message_id": str(message_id), "started_at": now_iso()})
                # S7.21：尽早关联 turn_id，让"运行中"的消息在库里就能按轮次追查
                await update_message(session, message_id, turn_id=events.turn_id)

                async def persist(*, stage: str, status: str, attempt: int = 1,
                                  latency_ms: int = 0, error_category: str = "",
                                  run_id: str = "") -> None:
                    """合并一条阶段更新；只有快照确实变化才写库（S7.21 的写库节流点）。

                    切到新阶段时把上一个还在跑的阶段收成 `succeeded`——否则快照的 `nodes`
                    里会留下永远 running 的旧阶段（阶段是顺序推进的：能走到下一步说明上一步成功）。
                    """
                    nonlocal snapshot, written
                    previous_stage = str(snapshot.get("stage") or "")
                    if (status == "running" and previous_stage and previous_stage != stage
                            and snapshot.get("status") == "running"):
                        snapshot = progress_tool.end(
                            snapshot, stage=previous_stage, status="succeeded",
                            attempt=int(snapshot.get("attempt") or 1))
                    snapshot = progress_tool.apply(snapshot, stage=stage, status=status,
                                                   attempt=attempt, latency_ms=latency_ms,
                                                   error_category=error_category, run_id=run_id)
                    marker = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                    if marker == written:
                        return
                    written = marker
                    await update_message(session, message_id, progress=dict(snapshot))

                async with build_http_client(cfg.llm.timeout_s) as client:
                    provider = build_turn_provider(cfg, client, tracer=tracer)
                    # 跨轮图片回落（S7.10 / S7.16 按组）：先把本会话最近一组图取出来，工具在
                    # "本轮没带图"时才有得用（用户说「上面那几张图」正是这种情况）
                    recent = await latest_attachments(session, session_id)
                    runner = build_tool_runner(session, cfg=cfg, client=client,
                                               session_id=session_id,
                                               attachments=attachments or [],
                                               dispatcher=dispatcher,
                                               caller=StructuredCaller(provider=provider),
                                               recent_attachments=recent,
                                               turn_id=events.turn_id, tracer=tracer)

                    async def on_event(event: str, payload: dict[str, Any]) -> None:
                        body = dict(payload)
                        if event != "text_delta":    # 每个 token 都补关联字段没有意义
                            body.setdefault("turn_id", events.turn_id)
                            body.setdefault("message_id", str(message_id))
                        if event == "stage":
                            await persist(stage=str(body.get("stage") or ""),
                                          status=str(body.get("status") or "running"),
                                          attempt=int(body.get("attempt") or 1),
                                          error_category=str(body.get("error_category") or ""),
                                          run_id=str(body.get("run_id") or ""))
                        elif event == "tool_progress" and body.get("stage_key"):
                            # 排队等待 / 素材同步 / 向量回填 / 五节点：worker 阶段随轮询可见
                            await persist(stage=str(body["stage_key"]), status="running",
                                          attempt=int(body.get("attempt") or 1))
                        elif event == "tool_finished" and body.get("stage_key"):
                            await persist(stage=str(body["stage_key"]),
                                          status=("succeeded"
                                                  if body.get("status") == "succeeded"
                                                  else "failed"),
                                          attempt=int(body.get("attempt") or 1),
                                          latency_ms=int(body.get("latency_ms") or 0))
                        await events.publish(event, body)

                    with tracer.turn(name="chat.turn",
                                     metadata={"session_id": str(session_id),
                                               "message_id": str(message_id)},
                                     turn_id=events.turn_id):
                        turn = await run_turn(cfg, provider=provider, history=history,
                                              user_text=user_text, run_tool=runner,
                                              attachment_note=attachment_note,
                                              on_event=on_event)
                        # 收尾阶段（S7.20）：正文已流式回吐，这一步只做写库与终态整理
                        await events.publish("stage", {
                            "turn_id": events.turn_id, "message_id": str(message_id),
                            "stage": "final_reply", "label": label_for("final_reply"),
                            "status": "running", "attempt": 1, "started_at": now_iso(),
                            "run_id": turn.run_id or None})
                        await persist(stage="final_reply", status="running",
                                      run_id=turn.run_id or "")
                        status = await _finish(session, events, turn, message_id=message_id,
                                               session_id=session_id, tracer=tracer,
                                               snapshot=snapshot)
                        tracer.finish_turn(status=status, totals=_turn_totals(turn),
                                           turn_id=events.turn_id,
                                           error=turn.error if status != "succeeded" else "")
        except Exception as exc:                 # noqa: BLE001 - 整轮失败要落库 + 广播，不炸进程
            logger.exception("对话轮次异常：%s", events.turn_id)
            error = f"{type(exc).__name__}: {exc}"[:200]
            category = error_category_of(exc)
            interrupted = progress_tool.interrupt(snapshot)
            await _mark_failed(factory, message_id=message_id, error=error,
                               progress=(dict(interrupted) if interrupted.get("status")
                                         else None))
            with tracer.turn(name="chat.turn", metadata={"failed": True},
                             turn_id=events.turn_id):
                await _fail(events, message_id=message_id, error=error,
                            error_category=category, tracer=tracer)
                tracer.finish_turn(status="failed", totals={}, turn_id=events.turn_id,
                                   error=error)
    finally:
        reset_turn_id(token)
        await events.close_publisher()
        await engine.dispose()


def _turn_totals(turn: ChatTurn) -> dict[str, Any]:
    """轮次汇总（只给 trace 收尾展示用；父层不参与核账，不跨层相加，约定 §7）。"""
    return {"tool_calls": len(turn.tool_calls),
            "prompt_tokens": turn.prompt_tokens, "completion_tokens": turn.completion_tokens,
            "cost_cny": turn.cost_cny, "latency_ms": turn.latency_ms,
            "usage_complete": True}


async def _mark_failed(factory: Any, *, message_id: uuid.UUID, error: str,
                       progress: dict[str, Any] | None = None) -> None:
    """兜底把 assistant 行标 `failed`（best-effort：连库都写不了就只剩事件）。"""
    fields: dict[str, Any] = {"status": "failed", "error": error}
    if progress is not None:
        fields["progress"] = dict(progress)
    with suppress(Exception):
        async with factory() as session:
            await update_message(session, message_id, **fields)


async def _finish(session: AsyncSession, events: TurnEvents, turn: ChatTurn, *,
                  message_id: uuid.UUID, session_id: uuid.UUID,
                  tracer: Tracer | None = None,
                  snapshot: dict[str, Any] | None = None) -> str:
    """写回消息并发终态事件（S7.20 起带 `turn_id` / `trace_id` / 阶段）；返回本轮终态。"""
    active = tracer if tracer is not None else NullTracer()
    trace_id = active.current_trace_id()
    if turn.status != "succeeded":
        # 失败阶段已由 `stage` 事件写进快照，这里不臆造 final_reply；补 trace 关联与终态
        final = progress_tool.normalize(snapshot)
        fields: dict[str, Any] = {"status": "failed",
                                  "error": turn.error or "这一轮失败了",
                                  "latency_ms": turn.latency_ms,
                                  "prompt_versions": turn.prompt_versions,
                                  "trace_id": trace_id or None}
        if final.get("status"):                  # 空快照不写，读侧会把它折成 null
            fields["progress"] = dict(final)
        await update_message(session, message_id, **fields)
        await events.publish("stage", {
            "turn_id": events.turn_id, "message_id": str(message_id),
            "stage": "final_reply", "label": label_for("final_reply"), "status": "failed",
            "attempt": 1, "updated_at": now_iso(),
            "error_category": turn.error_category or "internal"})
        await _fail(events, message_id=message_id, error=turn.error or "这一轮失败了",
                    error_category=turn.error_category or "internal", tracer=active)
        return "failed"
    final = progress_tool.end(progress_tool.normalize(snapshot), stage="final_reply",
                              status="succeeded")
    await update_message(session, message_id, content=turn.reply, status="succeeded",
                         tool_calls=turn.tool_calls, prompt_versions=turn.prompt_versions,
                         run_id=turn.run_id or None, cost_cny=turn.cost_cny,
                         latency_ms=turn.latency_ms,
                         prompt_tokens=turn.prompt_tokens,
                         completion_tokens=turn.completion_tokens,
                         trace_id=trace_id or None, progress=dict(final))
    await events.publish("stage", {
        "turn_id": events.turn_id, "message_id": str(message_id),
        "stage": "final_reply", "label": label_for("final_reply"), "status": "succeeded",
        "attempt": 1, "updated_at": now_iso(), "last_finished_stage": "final_reply",
        "run_id": turn.run_id or None})
    await events.publish("turn_finished", {
        "turn_id": events.turn_id, "session_id": str(session_id),
        "message_id": str(message_id), "status": "succeeded",
        "run_id": turn.run_id, "cost_cny": turn.cost_cny, "latency_ms": turn.latency_ms,
        "trace_id": trace_id, "last_finished_stage": "final_reply"})
    return "succeeded"


async def _fail(events: TurnEvents, *, message_id: uuid.UUID, error: str,
                error_category: str = "internal", tracer: Tracer | None = None) -> None:
    """失败终端事件：带轮次关联信息（失败阶段 / 安全类别 / trace），方便回看与排查。"""
    active = tracer if tracer is not None else NullTracer()
    await events.publish("error", {"turn_id": events.turn_id,
                                   "message_id": str(message_id),
                                   "code": "internal_error", "message": error,
                                   "stage": "", "error_category": error_category,
                                   "trace_id": active.current_trace_id()})

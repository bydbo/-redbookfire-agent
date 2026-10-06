"""对话轮次的运行器与事件总线（S7.4）。

用途：把「发一条消息」变成一次真正的对话——写好 `running` 的 assistant 行 → 跑对话图（S7.3）
      → 把结果写回消息 → 全程通过 Redis pub/sub 广播事件，SSE 端点订阅后转发给浏览器。
输入：`AppConfig`、会话与消息、历史、本轮文本与附件、投递器（把分析投给 Celery）。
输出：事件流（`turn_started` / `tool_started` / `tool_progress` / `tool_finished` /
      `text_delta` / `turn_finished` / `error`）与落库后的消息。

口径：

- **turn 跑在 detached asyncio 任务里**：浏览器断开只取消"转发"，不取消这一轮——结果照常落库。
  分析本来就在 Celery worker 跑；对话层的模型调用与图片解析接口同一先例（在 API 进程内等待）。
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
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core.errors import DependencyUnavailableError
from ..db.session import create_engine_from_config, create_session_factory
from ..services.chat import update_message
from ..services.chat_tools import TOOL_RUN_ANALYSIS, ToolContext, run_hotspot_analysis
from ..tools.http import build_http_client
from ..tools.llm import BaseProvider, build_provider
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

def build_turn_provider(cfg: AppConfig, client: Any) -> BaseProvider:
    """对话层用的模型 provider。"""
    return build_provider(cfg, client=client)


def build_tool_runner(session: AsyncSession, *, cfg: AppConfig, client: Any,
                      session_id: uuid.UUID, attachments: list[dict[str, Any]],
                      dispatcher: Any) -> Any:
    """工具执行器：把「跑完整分析」接到 S7.3 的工具上（其余工具名直接报错）。"""
    context = ToolContext(cfg=cfg, session=session, session_id=session_id,
                          enqueue=getattr(dispatcher, "enqueue", None), http=client)

    async def runner(name: str, args: dict[str, Any], *, on_progress: Any = None) -> Any:
        if name != TOOL_RUN_ANALYSIS:
            raise ValueError(f"未知工具：{name}")
        return await run_hotspot_analysis(context, args, attachments=attachments,
                                          on_progress=on_progress)

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
    """任务体：自建 engine / httpx，跑完这一轮并把结果写回消息、广播事件。"""
    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        try:
            async with factory() as session:
                await events.publish("turn_started", {
                    "turn_id": events.turn_id, "session_id": str(session_id),
                    "message_id": str(message_id)})
                async with build_http_client(cfg.llm.timeout_s) as client:
                    provider = build_turn_provider(cfg, client)
                    runner = build_tool_runner(session, cfg=cfg, client=client,
                                               session_id=session_id,
                                               attachments=attachments or [],
                                               dispatcher=dispatcher)

                    async def on_event(event: str, payload: dict[str, Any]) -> None:
                        await events.publish(event, payload)

                    turn = await run_turn(cfg, provider=provider, history=history,
                                          user_text=user_text, run_tool=runner,
                                          attachment_note=attachment_note, on_event=on_event)
                await _finish(session, events, turn, message_id=message_id,
                              session_id=session_id)
        except Exception as exc:                 # noqa: BLE001 - 整轮失败要落库 + 广播，不炸进程
            logger.exception("对话轮次异常：%s", events.turn_id)
            error = f"{type(exc).__name__}: {exc}"[:200]
            await _mark_failed(factory, message_id=message_id, error=error)
            await _fail(events, message_id=message_id, error=error)
    finally:
        await events.close_publisher()
        await engine.dispose()


async def _mark_failed(factory: Any, *, message_id: uuid.UUID, error: str) -> None:
    """兜底把 assistant 行标 `failed`（best-effort：连库都写不了就只剩事件）。"""
    with suppress(Exception):
        async with factory() as session:
            await update_message(session, message_id, status="failed", error=error)


async def _finish(session: AsyncSession, events: TurnEvents, turn: ChatTurn, *,
                  message_id: uuid.UUID, session_id: uuid.UUID) -> None:
    if turn.status != "succeeded":
        await update_message(session, message_id, status="failed",
                             error=turn.error or "这一轮失败了",
                             latency_ms=turn.latency_ms,
                             prompt_versions=turn.prompt_versions)
        await _fail(events, message_id=message_id, error=turn.error or "这一轮失败了")
        return
    await update_message(session, message_id, content=turn.reply, status="succeeded",
                         tool_calls=turn.tool_calls, prompt_versions=turn.prompt_versions,
                         run_id=turn.run_id or None, cost_cny=turn.cost_cny,
                         latency_ms=turn.latency_ms)
    await events.publish("turn_finished", {
        "turn_id": events.turn_id, "session_id": str(session_id),
        "message_id": str(message_id), "status": "succeeded",
        "run_id": turn.run_id, "cost_cny": turn.cost_cny, "latency_ms": turn.latency_ms})


async def _fail(events: TurnEvents, *, message_id: uuid.UUID, error: str) -> None:
    await events.publish("error", {"turn_id": events.turn_id,
                                   "message_id": str(message_id),
                                   "code": "internal_error", "message": error})

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
from sqlalchemy import CursorResult, text
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
# 取消收尾的写库上限（秒）：真有界——超时 / 再取消由陈旧对账兜底（OBS-09 / OBS-11）
CANCEL_FINALIZE_TIMEOUT_S = 5.0

# detached 任务必须自己保活，否则可能被 GC 掉（结果会"半路消失"）
_RUNNING: set[asyncio.Task[None]] = set()


def redis_url(cfg: AppConfig) -> str:
    """事件总线用的 REDIS_URL；缺失或前缀不对直接抛（配置契约 §2.1.1：P3 起必填）。"""
    url = (cfg.env_view.get("REDIS_URL") or "").strip()
    if not url.startswith(("redis://", "rediss://")):
        raise DependencyUnavailableError(
            "对话事件总线需要 REDIS_URL（redis:// 或 rediss://）",
            {"hint": "把 REDIS_URL 写进 config/.env 或环境变量（配置契约 §2.1.1）"},
        )
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
        except Exception:  # noqa: BLE001 - 旁路组件，绝不阻断主链路
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
        if queue is None:  # pragma: no cover - open() 之后必然有队列
            return
        try:
            async for message in self._pubsub.listen():
                if message.get("type") != "message":
                    continue
                item = _decode(message.get("data"))
                if item is not None:
                    await queue.put(item)
        except Exception:  # noqa: BLE001 - 订阅断了就让转发侧收到 None
            logger.warning("对话事件订阅中断：%s", self.turn_id, exc_info=True)
        finally:
            await queue.put(None)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """逐条产出事件（直到队列收到结束标记）；心跳由 SSE 生成器加。"""
        if self._queue is None:  # pragma: no cover - 调用方都会先 open()
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


def build_turn_provider(
    cfg: AppConfig, client: Any, *, tracer: Tracer | None = None
) -> BaseProvider:
    """对话层用的模型 provider：默认套一层追踪（S7.19）。

    与分析链路同一口径（`workflows/analysis.py`）：追松开时每次 supervisor 调用一个
    generation，`complete_with_tools` 的流式增量原样透传；三件套缺失 / 上报失败都不影响
    这一轮（`TracingProvider` 内部吞掉异常）。`cast` 只是给 mypy 一个交代——`TracingProvider`
    是鸭子类型，`core/` 不反向依赖 `tools/`。
    """
    inner = build_provider(cfg, client=client)
    return cast(
        BaseProvider,
        TracingProvider(
            inner,
            tracer if tracer is not None else get_tracer(cfg),
            capture=capture_content(cfg),
            price_in_per_m=cfg.llm.price_in_per_m,
            price_out_per_m=cfg.llm.price_out_per_m,
        ),
    )


def _enqueue_with_turn(enqueue: Any, turn_id: str) -> Any:
    """把 `turn_id` 一并传给支持它的投递器（S7.20）。

    只收 `job_id` 的旧替身（离线测试）原样返回——投递语义不变，只是不带业务标识；
    真实 `CeleryDispatcher.enqueue` 能收 `turn_id`，worker 侧据此绑定日志上下文。
    """
    if enqueue is None or not turn_id:
        return enqueue
    try:
        supports = "turn_id" in inspect.signature(enqueue).parameters
    except (TypeError, ValueError):  # 内建 / C 实现没有可查签名
        supports = False
    if not supports:
        return enqueue

    async def wrapped(job_id: str) -> None:
        await enqueue(job_id, turn_id=turn_id)

    return wrapped


def build_tool_runner(
    session: AsyncSession,
    *,
    cfg: AppConfig,
    client: Any,
    session_id: uuid.UUID,
    attachments: list[dict[str, Any]],
    dispatcher: Any,
    caller: Any = None,
    recent_attachments: list[dict[str, Any]] | None = None,
    turn_id: str = "",
    tracer: Tracer | None = None,
) -> Any:
    """工具执行器：按 `chat_tools.HANDLERS` 分派（S7.7a）——这里也不写具体工具名。

    `caller` 是给只读工具用的文本模型入口（拆解热点等）；`cache` 让同一轮内的同一件事
    只付一次钱（S7.7）。`recent_attachments` 是本会话最近**一组**图（S7.10 的跨轮图片回落，
    S7.16 改成整组）：这一轮没有附件时，用户说「上面那几张图」的工具调用才有东西可用。
    S7.20：`turn_id` 随分析投递传给 worker（绑日志上下文）；`tracer` 给每次工具调用开一个
    `tool.<name>` span——提交分析时的 traceparent 就取自它（worker 的父 span 口径）。
    """
    active = tracer if tracer is not None else NullTracer()
    context = ToolContext(
        cfg=cfg,
        session=session,
        session_id=session_id,
        enqueue=_enqueue_with_turn(getattr(dispatcher, "enqueue", None), turn_id),
        http=client,
        caller=caller,
        recent_attachments=list(recent_attachments or []),
    )

    async def runner(
        name: str, args: dict[str, Any], *, on_progress: Any = None
    ) -> Any:
        handler = chat_tools.HANDLERS.get(name)
        if handler is None:  # 图已按清单过滤过；这里只是兜底，不炸整轮
            return ToolOutcome(status="failed", error=f"未知工具：{name}")
        started = time.monotonic()
        with active.tool(
            name=f"tool.{name}", metadata={"tool": name, "turn_id": turn_id}
        ) as fields:
            try:
                outcome = await handler(
                    context, args, attachments=attachments, on_progress=on_progress
                )
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


def start_turn(
    cfg: AppConfig,
    events: TurnEvents,
    *,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    history: list[dict[str, Any]],
    user_text: str,
    attachment_note: str = "",
    attachments: list[dict[str, Any]] | None = None,
    dispatcher: Any = None,
) -> asyncio.Task[None]:
    """起一个 detached 任务跑完这一轮（调用方不 await；SSE 只订阅事件）。"""
    task = asyncio.create_task(
        run_turn_task(
            cfg,
            events,
            session_id=session_id,
            message_id=message_id,
            history=history,
            user_text=user_text,
            attachment_note=attachment_note,
            attachments=attachments or [],
            dispatcher=dispatcher,
        )
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task


#: 心跳续约（R5 / E7OBS2-06）：**只**前滚 `progress.updated_at`，不写整份阶段快照。
#: 行条件带 `status='running'`（终态后自然失配）与 turn 身份（串轮 / 迟到写覆盖不了新事实）。
_RENEW_ACTIVITY_SQL = text(
    "UPDATE chat_messages "
    "SET progress = jsonb_set(progress, '{updated_at}', to_jsonb(CAST(:ts AS text)), false) "
    "WHERE id = :message_id AND role = 'assistant' AND status = 'running' "
    "AND turn_id = CAST(:turn_id AS uuid) "
    "AND progress IS NOT NULL AND progress->>'stage' <> ''"
)
_RENEW_ACTIVITY_SQL_NO_TURN = text(
    "UPDATE chat_messages "
    "SET progress = jsonb_set(progress, '{updated_at}', to_jsonb(CAST(:ts AS text)), false) "
    "WHERE id = :message_id AND role = 'assistant' AND status = 'running' "
    "AND progress IS NOT NULL AND progress->>'stage' <> ''"
)


async def renew_turn_activity(
    session: AsyncSession, *, message_id: uuid.UUID, turn_id: str = ""
) -> int:
    """原子续约活动时间，返回受影响行数（R5 / E7OBS2-06）。

    与旧的"读快照副本 → 回写整份 JSON"不同：这条 UPDATE 只改 `updated_at` 一个键，
    并发的主流程刚写入的新阶段 / 终态不会被旧副本覆盖；`status='running'` 让终态之后的
    迟到心跳自然变成 0 行；`turn_id` 可解析为 uuid 时一并校验身份（不串轮）。
    """
    statement = _RENEW_ACTIVITY_SQL
    params: dict[str, Any] = {"message_id": message_id, "ts": now_iso()}
    try:
        params["turn_id"] = uuid.UUID(str(turn_id))
    except (TypeError, ValueError):  # 非 uuid 的测试 / 离线轮次：不校验身份
        statement = _RENEW_ACTIVITY_SQL_NO_TURN
    result = cast(CursorResult[Any], await session.execute(statement, params))
    return int(result.rowcount or 0)


async def run_turn_task(
    cfg: AppConfig,
    events: TurnEvents,
    *,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    history: list[dict[str, Any]],
    user_text: str,
    attachment_note: str = "",
    attachments: list[dict[str, Any]] | None = None,
    dispatcher: Any = None,
) -> None:
    """任务体：自建 engine / httpx，跑完这一轮并把结果写回消息、广播事件。

    S7.19 / S7.20：整轮包在**同一个** `chat.turn` span 里——成功、返回失败、异常、取消都在
    这一个业务入口内收尾，不再重建第二条轮次；`turn_id` 绑进日志上下文并在 `finally` 复位。
    S7.21：`turn_id` 与 `trace_id` **尽早落库**，阶段变化合并进 `chat_messages.progress`
    ——只在切换时写，不逐 token、不逐轮询。
    R2（OBS-03 / 04 / 09）：先按轮次结果分流（失败不做 `final_reply`）；消息字段与终端事件
    由**同一个终态快照**生成；`trace_id` 在进入 turn 后立即捕获并尽早写库；`CancelledError`
    单独收尾为 `interrupted`（类别 `cancelled`）后**原样重新抛出**，绝不吞掉停机控制流。
    """
    engine = create_engine_from_config(cfg)
    token = bind_turn_id(events.turn_id)
    tracer = get_tracer(cfg)
    snapshot = progress_tool.normalize({})
    written = ""
    factory = create_session_factory(engine)
    # R3（OBS-11）：心跳租约——周期把当前快照的 `updated_at` touch 一次（多进程可见的
    # 持久信号），陈旧对账据此区分"活跃长轮次"与"执行者消失"。不逐 token：间隔为
    # `turn_stale_seconds` 的 1/5（夹在 10–60 秒）。
    # R5（E7OBS2-06）：续约改为**原子 UPDATE**（只前滚 updated_at，不写整份快照）。
    interval = max(10.0, min(60.0, cfg.chat.turn_stale_seconds / 5.0))
    # R5（E7OBS2-01）：稳定边界账本——`run_turn` 注入同一份；取消 / 外层异常收尾时
    # 仍能读回已完成的工具过程、运行引用与账目（in-flight 那次拿不到，如实缺席）
    ledger: dict[str, Any] = {
        "records": [],
        "tool_calls": [],
        "versions": {},
        "run_id": "",
    }
    # R5（E7OBS2-05）：轮次时间窗（进入 run_turn 到返回）——trace 上落一份同边界时间戳，
    # 回放脚本据此把落库 latency_ms 与 trace 同口径比对，而不是拿含准备 / 收尾的入口 span
    window: dict[str, str] = {"run_started_at": "", "run_finished_at": ""}

    async def _heartbeat() -> None:
        """心跳租约（OBS-11 / R5-E7OBS2-06）：周期**原子续约**活动时间。

        只前滚 `progress.updated_at`（`renew_turn_activity`），不再用旧快照副本回写整个
        JSON——与主流程并发时覆盖不了刚写入的新阶段 / 终态；终态之后续约自然 0 行。
        单次故障不阻断这一轮（陈旧对账兜底），连续故障留安全诊断。
        """
        failures = 0
        while True:
            await asyncio.sleep(interval)
            try:
                async with create_session_factory(engine)() as session:
                    await renew_turn_activity(
                        session, message_id=message_id, turn_id=events.turn_id
                    )
                    await session.commit()
                failures = 0
            except Exception:  # noqa: BLE001 - 单次故障不阻断业务
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    logger.warning(
                        "心跳续约失败（第 %d 次，不阻断这一轮；交给陈旧对账兜底）",
                        failures,
                        exc_info=True,
                    )

    heartbeat_task = asyncio.create_task(_heartbeat())
    status = "failed"
    category = "internal"
    totals: dict[str, Any] = {}
    try:
        with tracer.turn(
            name="chat.turn",
            metadata={"session_id": str(session_id), "message_id": str(message_id)},
            turn_id=events.turn_id,
        ):
            trace_id = tracer.current_trace_id()  # 进入 turn 后尽早取（OBS-04）
            try:
                async with factory() as session:
                    await events.publish(
                        "turn_started",
                        {
                            "turn_id": events.turn_id,
                            "session_id": str(session_id),
                            "message_id": str(message_id),
                            "started_at": now_iso(),
                        },
                    )
                    # S7.21 / OBS-04：尽早关联 turn_id 与 trace_id——失败消息也保留 trace 关联
                    await update_message(
                        session,
                        message_id,
                        turn_id=events.turn_id,
                        trace_id=trace_id or None,
                    )

                    async def persist(
                        *,
                        stage: str,
                        status: str,
                        attempt: int = 1,
                        latency_ms: int = 0,
                        error_category: str = "",
                        run_id: str = "",
                        retries: int = 0,
                        job_id: str = "",
                    ) -> None:
                        """合并一条阶段更新；只有快照确实变化才写库（S7.21 的写库节流点）。

                        切到新阶段时把上一个还在跑的阶段收成 `succeeded`——否则快照的 `nodes`
                        里会留下永远 running 的旧阶段（阶段是顺序推进的：能走到下一步说明上一步成功；
                        **只在来源确实推进到下一阶段时才这样推断**，R3）。
                        R5（E7OBS2-02）：**身份冲突的迟到事件不触发该推断**——旧 run 的迟到
                        事件既不能收掉当前阶段，也不能把自己推为顶层。
                        """
                        nonlocal snapshot, written
                        incoming_run = str(run_id or "")
                        current_run = str(snapshot.get("run_id") or "")
                        same_execution = not (
                            incoming_run and current_run and incoming_run != current_run
                        )
                        previous_stage = str(snapshot.get("stage") or "")
                        if (
                            previous_stage
                            and previous_stage != stage
                            and snapshot.get("status") == "running"
                            and same_execution
                        ):
                            # 来源推进到下一阶段 ⇒ 上一阶段已完成（顺序流水线的可靠推断，R3）
                            # ——对 running 与终态更新都成立；推断只在这一条件下使用；
                            # 收尾带上当前执行的 run_id 身份，节点不会丢身份
                            snapshot = progress_tool.end(
                                snapshot,
                                stage=previous_stage,
                                status="succeeded",
                                attempt=int(snapshot.get("attempt") or 1),
                                run_id=str(snapshot.get("run_id") or ""),
                            )
                        snapshot = progress_tool.apply(
                            snapshot,
                            stage=stage,
                            status=status,
                            attempt=attempt,
                            retries=retries,
                            latency_ms=latency_ms,
                            error_category=error_category,
                            run_id=run_id,
                            job_id=job_id,
                        )
                        marker = json.dumps(
                            snapshot, ensure_ascii=False, sort_keys=True
                        )
                        if marker == written:
                            return
                        written = marker
                        await update_message(
                            session, message_id, progress=dict(snapshot)
                        )

                    async with build_http_client(cfg.llm.timeout_s) as client:
                        provider = build_turn_provider(cfg, client, tracer=tracer)
                        # 跨轮图片回落（S7.10 / S7.16 按组）：先把本会话最近一组图取出来，工具在
                        # "本轮没带图"时才有得用（用户说「上面那几张图」正是这种情况）
                        recent = await latest_attachments(session, session_id)
                        runner = build_tool_runner(
                            session,
                            cfg=cfg,
                            client=client,
                            session_id=session_id,
                            attachments=attachments or [],
                            dispatcher=dispatcher,
                            caller=StructuredCaller(provider=provider),
                            recent_attachments=recent,
                            turn_id=events.turn_id,
                            tracer=tracer,
                        )

                        async def on_event(event: str, payload: dict[str, Any]) -> None:
                            body = dict(payload)
                            if event != "text_delta":  # 每个 token 都补关联字段没有意义
                                body.setdefault("turn_id", events.turn_id)
                                body.setdefault("message_id", str(message_id))
                            if event == "stage":
                                await persist(
                                    stage=str(body.get("stage") or ""),
                                    status=str(body.get("status") or "running"),
                                    attempt=int(body.get("attempt") or 1),
                                    error_category=str(
                                        body.get("error_category") or ""
                                    ),
                                    run_id=str(body.get("run_id") or ""),
                                )
                            elif event == "tool_progress" and body.get("stage_key"):
                                # R3：worker 阶段的**结构化**更新（真实状态 / 源尝试号 / 重试 /
                                # 错误类别 / run 身份）——不再一律写 running，也便于区分
                                # failed / skipped / 重试；同一执行重复上报由 progress 单调合并
                                await persist(
                                    stage=str(body["stage_key"]),
                                    status=str(body.get("status") or "running"),
                                    attempt=int(body.get("attempt") or 1),
                                    retries=int(body.get("retries") or 0),
                                    error_category=str(
                                        body.get("error_category") or ""
                                    ),
                                    latency_ms=int(body.get("latency_ms") or 0),
                                    run_id=str(body.get("run_id") or ""),
                                    job_id=str(body.get("job_id") or ""),
                                )
                            elif event == "tool_finished" and body.get("stage_key"):
                                # R5（E7OBS2-03）：结构化 worker 阶段的收尾用 **worker 语义**
                                # ——worker 仍在 running（如超时返回）就不凭空收尾；收尾用
                                # worker 的尝试号 / 重试号 / 最后状态与最后已知耗时，工具
                                # 第几次调用（attempt）绝不用来覆盖阶段重试号。
                                if body.get("structured"):
                                    worker_status = str(body.get("worker_status") or "")
                                    if (
                                        worker_status
                                        in progress_tool.TERMINAL_STAGE_STATUSES
                                    ):
                                        await persist(
                                            stage=str(body["stage_key"]),
                                            status=worker_status,
                                            attempt=int(
                                                body.get("worker_attempt") or 1
                                            ),
                                            retries=int(
                                                body.get("worker_retries") or 0
                                            ),
                                            latency_ms=int(
                                                body.get("worker_latency") or 0
                                            ),
                                            run_id=str(body.get("run_id") or ""),
                                        )
                                else:
                                    # 旧式标签阶段（解析图片 / 拆解要素）：按工具本身上报收尾
                                    await persist(
                                        stage=str(body["stage_key"]),
                                        status=(
                                            "succeeded"
                                            if body.get("status") == "succeeded"
                                            else "failed"
                                        ),
                                        attempt=int(body.get("attempt") or 1),
                                        latency_ms=int(body.get("latency_ms") or 0),
                                        run_id=str(body.get("run_id") or ""),
                                    )
                            await events.publish(event, body)

                        # R2（OBS-03）：先按轮次结果分流——失败不做 `final_reply`
                        window["run_started_at"] = now_iso()
                        turn = await run_turn(
                            cfg,
                            provider=provider,
                            history=history,
                            user_text=user_text,
                            run_tool=runner,
                            attachment_note=attachment_note,
                            on_event=on_event,
                            ledger=ledger,
                        )
                        window["run_finished_at"] = now_iso()
                        if turn.status == "succeeded":
                            # 收尾阶段（S7.20）：正文已流式回吐，这一步只做写库与终态整理
                            await events.publish(
                                "stage",
                                {
                                    "turn_id": events.turn_id,
                                    "message_id": str(message_id),
                                    "stage": "final_reply",
                                    "label": label_for("final_reply"),
                                    "status": "running",
                                    "attempt": 1,
                                    "started_at": now_iso(),
                                    "run_id": turn.run_id or None,
                                },
                            )
                            await persist(
                                stage="final_reply",
                                status="running",
                                run_id=turn.run_id or "",
                            )
                        status = await _finish(
                            session,
                            events,
                            turn,
                            message_id=message_id,
                            session_id=session_id,
                            snapshot=snapshot,
                            trace_id=trace_id,
                        )
                        category = turn.error_category or (
                            "internal" if status != "succeeded" else ""
                        )
                        totals = {**_turn_totals(turn), **window}
            except asyncio.CancelledError:
                # R2（OBS-09）：真实取消 → 有界时间内尽力写 interrupted，然后原样继续传播
                # R5（E7OBS2-01）：稳定边界账本里已完成的工具过程 / 引用 / 账目一并尽力写回
                status, category = "interrupted", "cancelled"
                window["run_finished_at"] = window["run_finished_at"] or now_iso()
                await _finalize_turn(
                    factory,
                    events,
                    message_id=message_id,
                    snapshot=snapshot,
                    trace_id=trace_id,
                    status="interrupted",
                    error_category=category,
                    error="这一轮被取消（停机 / 任务取消），已按中断收尾",
                    budget_s=CANCEL_FINALIZE_TIMEOUT_S,
                    ledger=ledger,
                )
                totals = _ledger_totals(ledger, window)
                raise
            except Exception as exc:  # noqa: BLE001 - 整轮失败要落库 + 广播
                logger.exception("对话轮次异常：%s", events.turn_id)
                status, category = "failed", error_category_of(exc)
                window["run_finished_at"] = window["run_finished_at"] or now_iso()
                await _finalize_turn(
                    factory,
                    events,
                    message_id=message_id,
                    snapshot=snapshot,
                    trace_id=trace_id,
                    status="failed",
                    error_category=category,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                    ledger=ledger,
                )
                totals = _ledger_totals(ledger, window)
            finally:
                tracer.finish_turn(
                    status=status,
                    totals=totals,
                    turn_id=events.turn_id,
                    error="" if status == "succeeded" else category,
                )
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        reset_turn_id(token)
        await events.close_publisher()
        await engine.dispose()


def _turn_totals(turn: ChatTurn) -> dict[str, Any]:
    """轮次汇总（只给 trace 收尾展示用；父层不参与核账，不跨层相加，约定 §7）。

    R3（OBS-07）：完整性按**调用集合**上卷——`unknown` 计入 `usage_unknown_calls`，
    失败 / 取消轮次如实不完整；不再固定为真。
    """
    return {
        "tool_calls": len(turn.tool_calls),
        "prompt_tokens": turn.prompt_tokens,
        "completion_tokens": turn.completion_tokens,
        "cost_cny": turn.cost_cny,
        "latency_ms": turn.latency_ms,
        "usage_complete": bool(turn.usage_complete) and turn.status == "succeeded",
        "usage_unknown_calls": int(turn.usage_unknown_calls),
    }


def _ledger_snapshot(ledger: dict[str, Any] | None) -> dict[str, Any]:
    """稳定边界的已消费账目（R5 / E7OBS2-01）：取消 / 外层异常收尾时读出已完成记录。

    只包含**已完成**的调用（in-flight 的那次拿不到，如实缺席）；返回的字段与
    `ChatTurn` 同名，供 `_finalize_turn` 尽力写回已知部分。
    """
    records = list((ledger or {}).get("records") or [])
    tool_calls = list((ledger or {}).get("tool_calls") or [])
    both = [*records, *tool_calls]
    return {
        "tool_calls": tool_calls,
        "run_id": str((ledger or {}).get("run_id") or ""),
        "prompt_versions": dict((ledger or {}).get("versions") or {}),
        "cost_cny": round(sum(float(item.get("cost_cny") or 0) for item in both), 4),
        "prompt_tokens": sum(int(item.get("prompt_tokens") or 0) for item in both),
        "completion_tokens": sum(
            int(item.get("completion_tokens") or 0) for item in both
        ),
    }


def _ledger_totals(
    ledger: dict[str, Any] | None,
    window: dict[str, str],
    *,
    usage_complete: bool = False,
) -> dict[str, Any]:
    """取消 / 异常路径的 trace 汇总：账本已知部分 + 明确不完整标记 + 轮次时间窗。"""
    snapshot = _ledger_snapshot(ledger)
    return {
        "tool_calls": len(snapshot["tool_calls"]),
        "prompt_tokens": snapshot["prompt_tokens"],
        "completion_tokens": snapshot["completion_tokens"],
        "cost_cny": snapshot["cost_cny"],
        "usage_complete": usage_complete,
        "run_started_at": window.get("run_started_at", ""),
        "run_finished_at": window.get("run_finished_at", ""),
    }


def _terminal_snapshot(
    snapshot: dict[str, Any], *, status: str, error_category: str
) -> dict[str, Any]:
    """把当前快照收成终态（OBS-03）：只终结**确实存在**的当前阶段，绝不臆造阶段。

    - 当前阶段还在 `running`：终结为 `status`（`failed` / `interrupted`），带安全类别；
    - 已是终态：保留原样（缺类别时补上，供前端失败说明与排查使用）；
    - 从未有过任何阶段：原样返回（读侧会把空快照折成 `null`）。
    """
    data = progress_tool.normalize(snapshot)
    stage = str(data.get("stage") or "")
    if not stage:
        return data
    if data.get("status") == "running":
        return progress_tool.end(
            data,
            stage=stage,
            status=status,
            attempt=int(data.get("attempt") or 1),
            error_category=error_category,
            run_id=str(data.get("run_id") or ""),
        )
    if error_category and not data.get("error_category"):
        data["error_category"] = error_category
    return data


async def _finalize_turn(
    factory: Any,
    events: TurnEvents,
    *,
    message_id: uuid.UUID,
    snapshot: dict[str, Any],
    trace_id: str,
    status: str,
    error_category: str,
    error: str,
    budget_s: float | None = None,
    ledger: dict[str, Any] | None = None,
) -> None:
    """异常 / 取消的统一收尾（OBS-03 / 04 / 09；E7OBS2-01）：同一条终态快照生成消息字段与事件。

    - 消息：`status`（`failed` / `interrupted`）+ 错误文本 + **尽早捕获的 `trace_id`** +
      终态快照（真实失败阶段，不再无条件写 `final_reply`）；
      稳定边界账本里**已完成**的工具过程 / 运行引用 / 账目一并尽力写回；
    - 事件：`error` 带真实失败阶段与安全类别；
    - 写库尽力而为：连库都写不了就只留事件（陈旧对账兜底）；取消路径给写库加**有界**上限。
    """
    final = _terminal_snapshot(snapshot, status=status, error_category=error_category)
    fields: dict[str, Any] = {
        "status": status,
        "error": error,
        "trace_id": trace_id or None,
    }
    if ledger is not None:
        partial = _ledger_snapshot(ledger)
        if partial["tool_calls"]:
            fields["tool_calls"] = partial["tool_calls"]
        if partial["run_id"]:
            fields["run_id"] = partial["run_id"]
        if partial["prompt_versions"]:
            fields["prompt_versions"] = partial["prompt_versions"]
        fields["cost_cny"] = partial["cost_cny"]
        fields["prompt_tokens"] = partial["prompt_tokens"]
        fields["completion_tokens"] = partial["completion_tokens"]
    if final.get("status"):
        fields["progress"] = dict(final)

    async def _write() -> None:
        async with factory() as session:
            await update_message(session, message_id, **fields)

    try:
        if budget_s is None:
            await _write()
        else:
            async with asyncio.timeout(budget_s):
                await _write()
    except Exception:  # noqa: BLE001 - 尽力而为：写不了由陈旧对账兜底
        logger.warning(
            "轮次收尾写库失败（status=%s，交给陈旧对账兜底）：%s",
            status,
            events.turn_id,
        )
    await _fail(
        events,
        message_id=message_id,
        error=error,
        error_category=error_category,
        stage=str(final.get("stage") or ""),
        trace_id=trace_id,
    )


async def _finish(
    session: AsyncSession,
    events: TurnEvents,
    turn: ChatTurn,
    *,
    message_id: uuid.UUID,
    session_id: uuid.UUID,
    snapshot: dict[str, Any] | None = None,
    trace_id: str = "",
) -> str:
    """按轮次结果分流写回（OBS-03）；消息字段与终端事件由**同一个终态快照**生成。

    - 成功：`final_reply` 收尾（只覆盖写库与整理，不宣称生成耗时）→ `stage` 成功事件 +
      `turn_finished`；
    - 失败：终结真实失败阶段（不臆造 `final_reply`）→ 只发 `error` 事件（带真实失败阶段、
      安全类别与已捕获的 trace 关联）。
    """
    if turn.status != "succeeded":
        category = turn.error_category or "internal"
        final = _terminal_snapshot(
            progress_tool.normalize(snapshot), status="failed", error_category=category
        )
        # R3（OBS-10）：失败也保存已知用量与引用（工具状态 / prompt 版本 / run_id），
        # 未知部分由 finish_turn 的 usage_complete / usage_unknown_calls 单列
        # R5（E7OBS2-01）：**工具过程与运行引用**一并写回——刷新后失败消息仍能看到
        # 调过哪些工具、关联哪次运行，不再只有一行错误文本
        fields: dict[str, Any] = {
            "status": "failed",
            "error": turn.error or "这一轮失败了",
            "latency_ms": turn.latency_ms,
            "tool_calls": turn.tool_calls,
            "run_id": turn.run_id or None,
            "prompt_versions": turn.prompt_versions,
            "cost_cny": turn.cost_cny,
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
            "trace_id": trace_id or None,
        }
        if final.get("status"):  # 空快照不写，读侧会把它折成 null
            fields["progress"] = dict(final)
        await update_message(session, message_id, **fields)
        await _fail(
            events,
            message_id=message_id,
            error=turn.error or "这一轮失败了",
            error_category=category,
            stage=str(final.get("stage") or ""),
            trace_id=trace_id,
        )
        return "failed"
    final = progress_tool.end(
        progress_tool.normalize(snapshot), stage="final_reply", status="succeeded"
    )
    await update_message(
        session,
        message_id,
        content=turn.reply,
        status="succeeded",
        tool_calls=turn.tool_calls,
        prompt_versions=turn.prompt_versions,
        run_id=turn.run_id or None,
        cost_cny=turn.cost_cny,
        latency_ms=turn.latency_ms,
        prompt_tokens=turn.prompt_tokens,
        completion_tokens=turn.completion_tokens,
        trace_id=trace_id or None,
        progress=dict(final),
    )
    await events.publish(
        "stage",
        {
            "turn_id": events.turn_id,
            "message_id": str(message_id),
            "stage": "final_reply",
            "label": label_for("final_reply"),
            "status": "succeeded",
            "attempt": 1,
            "updated_at": now_iso(),
            "last_finished_stage": "final_reply",
            "run_id": turn.run_id or None,
        },
    )
    await events.publish(
        "turn_finished",
        {
            "turn_id": events.turn_id,
            "session_id": str(session_id),
            "message_id": str(message_id),
            "status": "succeeded",
            "run_id": turn.run_id,
            "cost_cny": turn.cost_cny,
            "latency_ms": turn.latency_ms,
            "trace_id": trace_id,
            "last_finished_stage": "final_reply",
        },
    )
    return "succeeded"


async def _fail(
    events: TurnEvents,
    *,
    message_id: uuid.UUID,
    error: str,
    error_category: str = "internal",
    stage: str = "",
    trace_id: str = "",
) -> None:
    """失败终端事件：真实失败阶段 + 安全类别 + 已捕获的 trace 关联（OBS-03 / 04）。

    `trace_id` 显式传入：异常展开后不再重读追踪上下文，也不依赖"当前 span"还在不在。
    """
    await events.publish(
        "error",
        {
            "turn_id": events.turn_id,
            "message_id": str(message_id),
            "code": "internal_error",
            "message": error,
            "stage": stage,
            "error_category": error_category,
            "trace_id": trace_id,
        },
    )

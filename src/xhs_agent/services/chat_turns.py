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
from dataclasses import dataclass, field
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy import CursorResult, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core import budget as budget_tool
from ..core import context_rules, model_attempts
from ..core import ledger as ledger_projection
from ..core import progress as progress_tool
from ..core.budget import BudgetDecision, BudgetPolicy
from ..core.context_policy import TurnContextPolicy
from ..core.context_rules import BLOCKING_REASONS
from ..core.errors import DependencyUnavailableError
from ..core.logging import bind_turn_id, reset_turn_id
from ..core.observations import record as record_observation
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
from ..db.models import ChatMessage
from ..db.session import create_engine_from_config, create_session_factory
from ..schemas import SchemaError
from ..services import chat_tools, context_state
from ..services.chat import latest_attachments, update_message
from ..services.chat_tools import ToolContext, ToolOutcome
from ..services.model_observations import observer as attempt_observer
from ..tools import prompt as prompt_tool
from ..tools.http import build_http_client
from ..tools.llm import BaseProvider, LLMError, StructuredCaller, build_provider
from ..tools.prompt import PromptError
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
    ledger: dict[str, Any] | None = None,
    policy: TurnContextPolicy | None = None,
    source_message_id: str = "",
    operation_binding: dict[str, Any] | None = None,
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
        turn_id=turn_id,
        policy=policy,
        source_message_id=source_message_id,
        operation_binding=dict(operation_binding or {}) or None,
    )
    scope_counts: dict[str, int] = {}

    def record_component(facts: dict[str, Any]) -> None:
        if ledger is None:
            return
        scope = str(facts.get("scope") or ledger.get("active_scope") or "")
        component = str(facts.get("component") or "")
        if not scope or not component:
            return
        components = ledger.setdefault("components", {})
        key = f"{scope}:{component}"
        previous = dict(components.get(key) or {})
        merged = {
            **previous,
            **facts,
            "scope": scope,
            "component": component,
            "tool": str(
                facts.get("tool") or previous.get("tool") or scope.rsplit(":", 1)[0]
            ),
        }
        components[key] = merged
        run_id = str(merged.get("run_id") or "")
        if run_id:
            ledger["run_id"] = run_id
        ledger.setdefault("versions", {}).update(merged.get("prompt_versions") or {})

    context.checkpoint = record_component

    async def runner(
        name: str,
        args: dict[str, Any],
        *,
        on_progress: Any = None,
        meta: dict[str, Any] | None = None,
    ) -> Any:
        handler = chat_tools.HANDLERS.get(name)
        if handler is None:  # 图已按清单过滤过；这里只是兜底，不炸整轮
            return ToolOutcome(status="failed", error=f"未知工具：{name}")
        scope_counts[name] = scope_counts.get(name, 0) + 1
        # E7OBS4-01：调用身份由图上登记后经 `meta` 传来（call_id 即内部 scope）；
        # 旧调用方 / 测试 runner 不传 meta 时退回"工具名:第几次"的兼容索引。
        scope = str((meta or {}).get("scope") or f"{name}:{scope_counts[name]}")
        context.checkpoint_scope = scope
        if ledger is not None:
            ledger["active_scope"] = scope
        started = time.monotonic()
        before_records = len(getattr(context.caller, "records", []))

        def known_tool_usage() -> dict[str, Any]:
            if context.caller is None:
                return {}
            records = list(getattr(context.caller, "records", []))[before_records:]
            if not records:
                return {}
            return {
                "cost_cny": chat_tools._records_cost(context.caller, before_records),
                "prompt_tokens": chat_tools._records_tokens(context.caller, before_records)[0],
                "completion_tokens": chat_tools._records_tokens(context.caller, before_records)[1],
                "usage_status": chat_tools._records_usage_status(context.caller, before_records),
                "prompt_versions": {str(row["task"]): _prompt_version(str(row["task"]))
                                    for row in records if row.get("task")},
            }

        with active.tool(
            name=f"tool.{name}", metadata={"tool": name, "turn_id": turn_id}
        ) as fields:
            try:
                bound_operation = str((context.operation_binding or {}).get("operation") or "")
                bound_tool = {"parse_clue_from_image": chat_tools.TOOL_PARSE_IMAGE,
                              "run_hotspot_analysis": chat_tools.TOOL_RUN_ANALYSIS,
                              "revise_draft": chat_tools.TOOL_REVISE_DRAFT}.get(bound_operation)
                if bound_tool and name in (chat_tools.TOOL_PARSE_IMAGE, chat_tools.TOOL_RUN_ANALYSIS,
                                           chat_tools.TOOL_REVISE_DRAFT) and name != bound_tool:
                    outcome = ToolOutcome(status="failed", error="本轮工具与用户明确选择的操作不一致",
                                          payload={"reason": "operation_binding_mismatch"})
                else:
                    outcome = await handler(
                        context, args, attachments=attachments, on_progress=on_progress
                    )
            except BaseException as exc:
                known = known_tool_usage()
                if known:
                    record_component({"scope": scope, "component": "tool_llm", "tool": name, **known})
                fields.level = LEVEL_ERROR
                fields.status_message = error_category_of(exc)
                raise
            known = known_tool_usage()
            if known and not any(key in outcome.payload for key in ("cost_cny", "prompt_tokens", "completion_tokens")):
                outcome.payload.update({key: value for key, value in known.items() if key != "prompt_versions"})
                outcome.prompt_versions.update(known["prompt_versions"])
            fields.metadata["status"] = outcome.status
            fields.metadata["latency_ms"] = int((time.monotonic() - started) * 1000)
            if outcome.status != "succeeded":
                # 只记安全类别：错误正文可能含用户原文 / 路径，不进追踪元数据（约定 §9）
                fields.level = LEVEL_ERROR
                fields.status_message = "tool_failed"
            if ledger is not None:
                payload = outcome.payload
                has_totals = any(
                    key in payload
                    for key in ("cost_cny", "prompt_tokens", "completion_tokens")
                )
                if has_totals:
                    record_component(
                        {
                            "scope": scope,
                            "component": "tool_total",
                            "aggregate": True,
                            "tool": name,
                            "status": outcome.status,
                            "cost_cny": float(payload.get("cost_cny") or 0),
                            "prompt_tokens": int(payload.get("prompt_tokens") or 0),
                            "completion_tokens": int(
                                payload.get("completion_tokens") or 0
                            ),
                            "usage_status": str(payload.get("usage_status") or ""),
                            "run_id": str(payload.get("run_id") or ""),
                            "job_id": str(payload.get("job_id") or ""),
                        }
                    )
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
    user_message_id: uuid.UUID | None = None,
) -> asyncio.Task[None]:
    """起一个 detached 任务跑完这一轮（调用方不 await；SSE 只订阅事件）。

    `user_message_id` 是本轮用户消息的**真实 ID**（S7.24）：状态抽取的证据与来源都要落到
    它上面，不能只存一句模型生成的解释。
    """
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
            user_message_id=user_message_id,
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


@dataclass
class ContextPrep:
    """轮次入口准备好的上下文（S7.24 / S7.26；v2 §3.4 状态化）。

    `status` 是本轮的准入结论：调用方对非 `ready` 的结果**不得启动依赖工具**——

    - `ready`：正常进入对话图；
    - `confirmation_required`：存在阻断当前任务的待确认项（含恢复未完成），只回答确认提示；
    - `conflict`：状态 CAS 失败或恢复/冲突未解决，返回可重试提示；
    - `invalid`：增量整批验证失败或来源无效；
    - `overflow`：必需内容超出预算；
    - `off`：模式关闭，保持旧行为。
    """

    mode: str = "off"
    status: str = "off"
    state_text: str = ""
    grouped: bool = False
    policy: BudgetPolicy | None = None
    blocking_reply: str = ""
    versions: dict[str, int] = field(default_factory=dict)
    error: str = ""
    error_category: str = ""
    turn_policy: Any = None

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "enforce")

    @property
    def ready(self) -> bool:
        return self.status == "ready"


async def _context_refs(
    session: AsyncSession, session_id: uuid.UUID
) -> list[dict[str, Any]]:
    """本轮可引用的对象（服务端已验证）：最近一次运行的热点位置。"""
    from ..services.chat import latest_run_id
    from ..services.runs import load_run

    run_id = await latest_run_id(session, session_id)
    if not run_id:
        return []
    try:
        detail = await load_run(session, uuid.UUID(run_id))
    except ValueError:
        return []
    if not detail:
        return []
    return context_state.objects_for_refs(
        run_id, list(detail.get("hotspots") or [])
    )


async def _load_source_binding(
    session: AsyncSession, message_id: uuid.UUID | None, *, session_id: uuid.UUID
) -> dict[str, Any] | None:
    """读取来源用户消息里持久化的操作绑定（A04：只读一次，不采信模型改写）。"""
    if message_id is None:
        return None
    try:
        value = (
            await session.execute(
                select(ChatMessage.operation_binding).where(
                    ChatMessage.id == message_id,
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == "user",
                )
            )
        ).one_or_none()
    except Exception as exc:
        raise DependencyUnavailableError("无法读取本轮操作绑定，请重试") from exc
    if value is None:
        raise DependencyUnavailableError("本轮来源用户消息不存在或不属于当前会话")
    value = value[0]
    if isinstance(value, dict) and value:
        return dict(value)
    return None


async def _prepare_context(
    session: AsyncSession,
    cfg: AppConfig,
    *,
    provider: BaseProvider,
    session_id: uuid.UUID,
    user_message_id: uuid.UUID,
    user_text: str,
    turn_id: str,
    ledger: dict[str, Any],
    policy: TurnContextPolicy | None = None,
    target_object_key: str = "",
) -> ContextPrep:
    """历史裁剪前的状态抽取与验证（S7.24；v2 §3.4 状态化准入）。

    返回的 `status` 决定本轮能不能启动依赖状态的工具（E7CTX-03）：

    - 遗留冲突（上一轮遗留的 conflict / ambiguous / scope 提案）同样阻断；
    - CAS 失败 → `conflict`：禁止悄悄读取新快照后继续原任务；
    - 增量整批验证失败 / 来源无效 → `invalid`；
    - 恢复未完成（pending / running / awaiting_confirmation）→ `confirmation_required`；
    - off 模式直接返回 `off`，保持旧行为。
    """
    if policy is None:
        policy = TurnContextPolicy.from_config(cfg, str(session_id))
    budget_policy = BudgetPolicy(
        application_limit=policy.application_limit,
        model_window=policy.model_window,
        mode=policy.mode,
        output_tokens=policy.output_tokens,
    )
    prep = ContextPrep(
        mode=policy.mode,
        status="off" if policy.mode == "off" else "ready",
        grouped=policy.grouped_history,
        policy=budget_policy,
        turn_policy=policy,
    )
    stored = await context_state.load(session, session_id)
    if stored is None:
        return prep

    # ---- 恢复未完成：不进入依赖旧状态的业务（v2 §3.5）----
    recovery_status = str((stored.recovery or {}).get("status") or "complete")
    if policy.blocks_on_unresolved and recovery_status in (
        "pending", "running", "awaiting_confirmation", "failed", "interrupted",
    ):
        prep.status = "confirmation_required"
        prep.blocking_reply = (
            "这个会话的历史要求还没有恢复完成（当前状态："
            f"{recovery_status}）。请先在「当前目标与限制」面板里完成恢复或确认要求，"
            "再继续；查看历史和处理恢复不受影响。"
        )
        return prep

    # ---- 遗留冲突：每轮检查全部仍适用的未解决冲突（E7CTX-03，不限于本轮新增）----
    blocking_all = context_rules.blocking_proposals(stored.snapshot, topic_id=stored.topic_id,
        object_keys=(target_object_key,) if target_object_key else ())
    if policy.blocks_on_unresolved and blocking_all and not policy.enforce:
        prep.status = "confirmation_required"
        prep.blocking_reply = _blocking_reply(stored, None)
        return prep

    if not prep.enabled:
        return prep

    # A06：抽取及其结构修复**每一次真实请求**都要过预算入口——挂在 StructuredCaller 的
    # before_request 上（而不是只在循环前检查一次）；每次请求各记一条预算事件。
    extract_checks = {"n": 0}
    extract_version = _prompt_version(context_state.TASK_ID)

    async def before_extract_request(call: Any) -> None:
        extract_checks["n"] += 1
        messages = call.messages or [
            {"role": "system", "content": call.system},
            {"role": "user", "content": call.user},
        ]
        decision = budget_tool.decide(
            messages=messages,
            tools=call.tools,
            output_reserve=int(call.max_tokens or cfg.context.extract_max_tokens),
            application_limit=policy.application_limit,
            model_window=policy.model_window,
            mode=policy.mode,
            protected=("本轮用户原文", "现有状态"),
        )
        await record_observation(
            session,
            event_type="context_assembled",
            session_id=session_id,
            key=f"{turn_id}:extract:{extract_checks['n']}",
            payload={**decision.as_event(), "effective_mode": policy.mode,
                     "kind": "extract_request", "request_index": extract_checks["n"]},
            turn_id=turn_id,
            prompt_version=f"context_extract:v{extract_version}",
            policy_version=f"context:{policy.mode}",
        )
        await session.commit()
        if policy.enforce and not decision.allowed:
            raise budget_tool.BudgetError(
                "状态抽取的请求超出预算，本轮不发出该请求",
                reason="required_context_overflow",
            )

    caller = StructuredCaller(
        provider=provider,
        max_repairs=int(cfg.context.extract_repair_max),
        before_request=before_extract_request,
    )
    refs = await _context_refs(session, session_id)
    try:
        delta, _result = await context_state.extract(
            cfg, caller, user_text=user_text, stored=stored, refs=refs
        )
        applied = await context_state.apply_delta(
            session,
            cfg,
            stored,
            delta,
            message_id=str(user_message_id),
            turn_id=turn_id,
            user_text=user_text,
            mode=policy.mode,
            refs=refs,
        )
    except (LLMError, SchemaError, PromptError, budget_tool.BudgetError) as exc:
        logger.warning("上下文抽取失败（mode=%s）：%s", policy.mode, type(exc).__name__)
        if policy.enforce:
            overflow = isinstance(exc, budget_tool.BudgetError)
            prep.status = "overflow" if overflow else "invalid"
            prep.error = "这一轮的必需上下文与输出预留超出预算，请缩小范围或调整预算后重试。" if overflow else (
                "这一轮没能确认上下文状态（抽取或校验失败），为避免按错误的前提继续，"
                "请重试；如果反复失败，可以把要求写得更具体一些"
            )
            prep.error_category = error_category_of(exc)
        return prep
    finally:
        # 抽取是一次真实调用：已发生的消费与版本一律计入本轮（失败也不丢）
        ledger.setdefault("records", []).extend(caller.records)
        extract_version = _prompt_version(context_state.TASK_ID)
        for record in caller.records:
            task = str(record.get("task") or "")
            if task:
                ledger.setdefault("versions", {})[task] = _prompt_version(task)
        if caller.records:
            # A11：成功 / 失败 / 取消都要带真实抽取 prompt 版本，不能只有 supervisor
            ledger["versions"][context_state.TASK_ID] = extract_version
            prep.versions["context_extract"] = extract_version

    # ---- 抽取后的准入判定 ----
    latest = await context_state.load(session, session_id)
    if latest is None:
        return prep
    prep.state_text = context_state.state_prompt(
        latest, object_keys=(target_object_key,) if target_object_key else (),
    ) if policy.enforce else ""
    if applied.conflict:
        # E7CTX-03：CAS 失败 → 停止依赖本次增量的执行，不悄悄读新快照继续
        prep.status = "conflict"
        prep.blocking_reply = (
            "这一轮的状态更新没有落库（状态版本已变化），为避免基于旧前提执行，"
            "请重试；刷新后可见最新状态。"
        )
        return prep
    if applied.reason in ("invalid_delta", "source_message_invalid"):
        prep.status = "invalid"
        prep.blocking_reply = (
            "这一轮的要求没能通过校验（证据与原文对不上或来源无效），"
            "我不会按它执行；请把要求再说明清楚一点。"
        )
        return prep
    blocking_all = context_rules.blocking_proposals(latest.snapshot, topic_id=latest.topic_id,
        object_keys=(target_object_key,) if target_object_key else ())
    if policy.blocks_on_unresolved and blocking_all:
        prep.status = "confirmation_required"
        prep.blocking_reply = _blocking_reply(latest, applied)
        return prep
    prep.status = "ready"
    return prep


def _prompt_version(task_id: str) -> int:
    """prompt 资产的真实版本号（A11：未调用任务不写版本，写就必须是真实版本）。"""
    try:
        return int(prompt_tool.load(task_id).version)
    except Exception:  # noqa: BLE001 - 资产缺失不阻断收尾，退回 v1 并由账本如实标注
        return 1


def _blocking_reply(stored: Any, applied: Any) -> str:
    """阻断提示：说清是哪条要求需要确认，并说明确认前不会继续执行。"""
    lines = ["这条要求我先不往下做——有一处需要你确认："]
    proposals = []
    snapshot = getattr(applied, "snapshot", None)
    if snapshot is not None:
        proposals = [item for item in snapshot.proposals if item.reason in BLOCKING_REASONS]
    if proposals:
        for item in proposals[:3]:
            lines.append(f"- {item.kind}：{item.value}（{item.reason}）")
    else:
        lines.append("- 新要求与当前生效的限制可能冲突，或指代不明确")
    lines.append("确认后我再继续；也可以直接告诉我新的要求。")
    _ = stored
    return "\n".join(lines)


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
    user_message_id: uuid.UUID | None = None,
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
        "calls": {},
        "tool_calls": [],
        "components": {},
        "versions": {},
        "run_id": "",
    }
    observation_token = model_attempts.bind(attempt_observer(
        factory, cfg, session_id=str(session_id), turn_id=events.turn_id,
        policy=cfg.context.effective_mode(str(session_id)),
        interrupted_records=ledger["records"]))
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
                        source_id: str = "",
                        stage_instance_id: str = "",
                        source_seq: int = 0,
                        progress_revision: int = 0,
                        generation: int = 0,
                    ) -> dict[str, Any]:
                        """合并一条阶段更新；只有快照确实变化才写库（S7.21 的写库节流点）。

                        E7OBS4-03：**先裁决、后更新**——不再在合并前把"上一阶段"推断成
                        `succeeded`。旧做法在守卫之前就改写了快照（并把来源退回 run_id），
                        迟到的旧来源事件因此绕过了序号比较，还能触发一次伪造的成功收尾。
                        前序阶段是否完成，只由来源端的明确终态表达；收不到就如实保持
                        "未结束 / 未知"，不补造成功（需要收敛时用真实的失败 / 中断语义）。
                        """
                        nonlocal snapshot, written
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
                            source_id=source_id,
                            stage_instance_id=stage_instance_id,
                            source_seq=source_seq,
                            progress_revision=progress_revision,
                            generation=generation,
                        )
                        marker = json.dumps(
                            snapshot, ensure_ascii=False, sort_keys=True
                        )
                        if marker == written:
                            return dict(snapshot)
                        written = marker
                        await update_message(
                            session, message_id, progress=dict(snapshot)
                        )
                        return dict(snapshot)

                    async with build_http_client(cfg.llm.timeout_s) as client:
                        provider = build_turn_provider(cfg, client, tracer=tracer)
                        # E7CTX-15：模式在轮次入口**一次**解析，传给状态 / 版本 / 预算 / 缓存 / 工具
                        turn_policy = TurnContextPolicy.from_config(cfg, str(session_id))
                        tool_calls_budget = {"n": 0}

                        async def before_tool_request(call: Any) -> None:
                            """工具内部模型调用的统一预算入口（E7CTX-09）。

                            挂在 `StructuredCaller.before_request` 上：改稿、拆解、覆盖预览
                            的**每次实际请求**（含结构修复）都先检查；enforce 超限抛
                            `BudgetError`，请求不发出。
                            """
                            tool_calls_budget["n"] += 1
                            messages = call.messages or [
                                {"role": "system", "content": call.system},
                                {"role": "user", "content": call.user},
                            ]
                            decision = budget_tool.decide(
                                messages=messages,
                                tools=None,
                                output_reserve=int(call.max_tokens or cfg.llm.max_tokens),
                                application_limit=turn_policy.application_limit,
                                model_window=turn_policy.model_window,
                                mode=turn_policy.mode,
                                protected=("本轮用户原文", "有效状态", "基础版本"),
                            )
                            await record_observation(
                                session,
                                event_type="context_assembled",
                                session_id=session_id,
                                key=f"{events.turn_id}:tool:{tool_calls_budget['n']}",
                                payload={**decision.as_event(),
                                         "effective_mode": turn_policy.mode,
                                         "kind": "tool_internal",
                                         "task": str(call.task)},
                                turn_id=events.turn_id,
                                prompt_version=f"{call.task}",
                                policy_version=f"context:{turn_policy.mode}",
                            )
                            if turn_policy.enforce and not decision.allowed:
                                raise budget_tool.BudgetError(
                                    "工具调用的必需内容超出请求预算，本轮不发出该请求",
                                    reason="required_context_overflow",
                                )

                        # 跨轮图片回落（S7.10 / S7.16 按组）：先把本会话最近一组图取出来，工具在
                        # "本轮没带图"时才有得用（用户说「上面那几张图」正是这种情况）
                        recent = await latest_attachments(session, session_id)
                        # A04：轮次按**来源用户消息 ID**读取一次已持久化的可信绑定，
                        # 随 ToolContext 传给工具；不从模型参数重新生成。
                        source_binding = await _load_source_binding(
                            session, user_message_id, session_id=session_id
                        )
                        runner = build_tool_runner(
                            session,
                            cfg=cfg,
                            client=client,
                            session_id=session_id,
                            attachments=attachments or [],
                            dispatcher=dispatcher,
                            caller=StructuredCaller(
                                provider=provider, before_request=before_tool_request
                            ),
                            recent_attachments=recent,
                            turn_id=events.turn_id,
                            tracer=tracer,
                            ledger=ledger,
                            policy=turn_policy,
                            source_message_id=str(user_message_id or ""),
                            operation_binding=source_binding,
                        )
                        # S7.24：历史裁剪**之前**做一次结构化状态抽取（输入是本轮原文而不是
                        # 被裁过的历史）。mode=off 时不抽取，保持旧行为。
                        context_prep = await _prepare_context(
                            session,
                            cfg,
                            provider=provider,
                            session_id=session_id,
                            user_message_id=user_message_id or message_id,
                            user_text=user_text,
                            turn_id=events.turn_id,
                            ledger=ledger,
                            policy=turn_policy,
                            target_object_key=str((source_binding or {}).get("object_key") or ""),
                        )
                        budget_calls = {"n": 0}

                        async def on_budget(decision: BudgetDecision) -> None:
                            """`context_assembled` 观测：每次实际请求前一条（含裁剪结果）。"""
                            budget_calls["n"] += 1
                            await record_observation(
                                session,
                                event_type="context_assembled",
                                session_id=session_id,
                                key=f"{events.turn_id}:{budget_calls['n']}",
                                payload={
                                    **decision.as_event(),
                                    "effective_mode": context_prep.mode,
                                },
                                turn_id=events.turn_id,
                                policy_version=f"context:{context_prep.mode}",
                            )
                            # 预算观测带会话外键；发送前结束短事务，不持锁等待模型网络响应。
                            await session.commit()

                        async def on_event(event: str, payload: dict[str, Any]) -> None:
                            body = dict(payload)
                            if event != "text_delta":  # 每个 token 都补关联字段没有意义
                                body.setdefault("turn_id", events.turn_id)
                                body.setdefault("message_id", str(message_id))
                                # E7OBS4-03：对话轮次自己的阶段（supervisor / 收尾）显式声明
                                # 来源。缺省会由 `begin()` 退回 run_id，多轮 / 多来源同阶段
                                # 时身份比较失真；worker 结构化更新自带来源，不受影响。
                                body.setdefault(
                                    "source_id", f"chat:{events.turn_id}"
                                )
                            if event == "stage":
                                merged = await persist(
                                    stage=str(body.get("stage") or ""),
                                    status=str(body.get("status") or "running"),
                                    attempt=int(body.get("attempt") or 1),
                                    error_category=str(
                                        body.get("error_category") or ""
                                    ),
                                    run_id=str(body.get("run_id") or ""),
                                    source_id=str(body.get("source_id") or ""),
                                    stage_instance_id=str(
                                        body.get("stage_instance_id") or ""
                                    ),
                                    source_seq=int(body.get("source_seq") or 0),
                                    progress_revision=int(
                                        body.get("progress_revision") or 0
                                    ),
                                    generation=int(body.get("generation") or 0),
                                )
                                body["progress"] = merged
                                body["progress_revision"] = int(
                                    merged.get("progress_revision") or 0
                                )
                            elif event == "tool_progress" and body.get("stage_key"):
                                # R3：worker 阶段的**结构化**更新（真实状态 / 源尝试号 / 重试 /
                                # 错误类别 / run 身份）——不再一律写 running，也便于区分
                                # failed / skipped / 重试；同一执行重复上报由 progress 单调合并
                                merged = await persist(
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
                                    source_id=str(body.get("source_id") or ""),
                                    stage_instance_id=str(
                                        body.get("stage_instance_id") or ""
                                    ),
                                    source_seq=int(body.get("source_seq") or 0),
                                    progress_revision=int(
                                        body.get("progress_revision") or 0
                                    ),
                                    generation=int(body.get("generation") or 0),
                                )
                                body["progress"] = merged
                                body["progress_revision"] = int(
                                    merged.get("progress_revision") or 0
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
                                        merged = await persist(
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
                                            source_id=str(body.get("source_id") or ""),
                                            stage_instance_id=str(
                                                body.get("stage_instance_id") or ""
                                            ),
                                            source_seq=int(body.get("source_seq") or 0),
                                            progress_revision=int(
                                                body.get("progress_revision") or 0
                                            ),
                                            generation=int(body.get("generation") or 0),
                                        )
                                        body["progress"] = merged
                                        body["progress_revision"] = int(
                                            merged.get("progress_revision") or 0
                                        )
                                else:
                                    # 旧式标签阶段（解析图片 / 拆解要素）：按工具本身上报收尾
                                    merged = await persist(
                                        stage=str(body["stage_key"]),
                                        status=(
                                            "succeeded"
                                            if body.get("status") == "succeeded"
                                            else "failed"
                                        ),
                                        attempt=int(body.get("attempt") or 1),
                                        latency_ms=int(body.get("latency_ms") or 0),
                                        run_id=str(body.get("run_id") or ""),
                                        # E7OBS4-03：收尾同样带来源 / 实例 / 序号——
                                        # 漏传时 `end()` 会把来源退回 run_id，
                                        # 迟到事件的身份比较随之失真
                                        source_id=str(body.get("source_id") or ""),
                                        stage_instance_id=str(
                                            body.get("stage_instance_id") or ""
                                        ),
                                        source_seq=int(body.get("source_seq") or 0),
                                        generation=int(body.get("generation") or 0),
                                    )
                                    body["progress"] = merged
                                    body["progress_revision"] = int(
                                        merged.get("progress_revision") or 0
                                    )
                            await events.publish(event, body)

                        # R2（OBS-03）：先按轮次结果分流——失败不做 `final_reply`
                        window["run_started_at"] = now_iso()
                        if context_prep.error and context_prep.status in ("invalid", "overflow") \
                                and not context_prep.blocking_reply:
                            # 抽取 / 校验失败：本轮停止依赖新状态的任务，不静默继续
                            turn = ChatTurn(
                                status="failed",
                                error=context_prep.error,
                                error_category=context_prep.error_category or "internal",
                                prompt_versions=dict(context_prep.versions),
                            )
                        elif context_prep.status in (
                            "confirmation_required", "conflict", "invalid",
                        ) and context_prep.blocking_reply:
                            # E7CTX-03：待确认 / 冲突 / 校验失败——只回复处理提示，
                            # 不启动依赖状态的工具（不能一边询问一边改稿）
                            turn = ChatTurn(
                                reply=context_prep.blocking_reply,
                                status="succeeded",
                                prompt_versions=dict(context_prep.versions),
                            )
                        else:
                            turn = await run_turn(
                                cfg,
                                provider=provider,
                                history=history,
                                user_text=user_text,
                                run_tool=runner,
                                attachment_note=attachment_note,
                                on_event=on_event,
                                ledger=ledger,
                                state_text=context_prep.state_text,
                                grouped=context_prep.grouped,
                                budget_policy=context_prep.policy,
                                on_budget=on_budget,
                            )
                        window["run_finished_at"] = now_iso()
                        if turn.status == "succeeded":
                            # 收尾阶段（S7.20）：正文已流式回吐，这一步只做写库与终态整理
                            # E7OBS4-04：先合并快照、再发事件——发出的事件必须带权威 progress，
                            # 不能出现"只携带原始阶段"的最终事件（前端不得自拼状态）
                            closing = await persist(
                                stage="final_reply",
                                status="running",
                                run_id=turn.run_id or "",
                                # 收尾阶段属于对话轮次，来源显式声明；缺省会被 end() 退回 run_id
                                source_id=f"chat:{events.turn_id}",
                                stage_instance_id=f"chat:{events.turn_id}:final_reply",
                                source_seq=1,
                            )
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
                                    "source_id": f"chat:{events.turn_id}",
                                    "stage_instance_id": f"chat:{events.turn_id}:final_reply",
                                    "source_seq": 1,
                                    "progress": dict(closing),
                                    "progress_revision": int(
                                        closing.get("progress_revision") or 0
                                    ),
                                },
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
                totals = _ledger_totals(ledger, window, open_status="interrupted")
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
                totals = _ledger_totals(ledger, window, open_status="failed")
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
        model_attempts.reset(observation_token)
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


def _ledger_snapshot(
    ledger: dict[str, Any] | None, *, open_status: str = "interrupted"
) -> dict[str, Any]:
    """稳定边界的账目投影（先由 `core/ledger.project` 统一计算，E7OBS4-02）。

    取消 / 外层异常 / 工作流内失败共用同一份纯函数投影：已完成调用取 aggregate，未完成
    调用由调用开始时登记的真实身份 + 组件已知消费生成部分记录（状态如实为
    failed / interrupted），不虚构参数、不补造成功。
    """
    return ledger_projection.project(ledger, open_status=open_status)


def _ledger_totals(
    ledger: dict[str, Any] | None,
    window: dict[str, str],
    *,
    usage_complete: bool = False,
    open_status: str = "interrupted",
) -> dict[str, Any]:
    """取消 / 异常路径的 trace 汇总：账本已知部分 + 明确不完整标记 + 轮次时间窗。"""
    snapshot = _ledger_snapshot(ledger, open_status=open_status)
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
        # E7OBS4-03：收尾保留当前阶段的来源 / 实例 / 序号——缺字段时 end() 会把来源退回
        # run_id，迟到事件的身份比较随之失真（验收 4.1 的根因之一）。
        return progress_tool.end(
            data,
            stage=stage,
            status=status,
            attempt=int(data.get("attempt") or 1),
            error_category=error_category,
            run_id=str(data.get("run_id") or ""),
            job_id=str(data.get("job_id") or ""),
            source_id=str(data.get("source_id") or ""),
            stage_instance_id=str(data.get("stage_instance_id") or ""),
            source_seq=int(data.get("source_seq") or 0),
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
        partial = _ledger_snapshot(
            ledger, open_status="failed" if status == "failed" else "interrupted"
        )
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
        progress=final,
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
            progress=final,
        )
        return "failed"
    current = progress_tool.normalize(snapshot)
    final = progress_tool.end(
        current,
        stage="final_reply",
        status="succeeded",
        attempt=int(current.get("attempt") or 1),
        run_id=str(current.get("run_id") or turn.run_id or ""),
        job_id=str(current.get("job_id") or ""),
        source_id=str(current.get("source_id") or ""),
        stage_instance_id=str(current.get("stage_instance_id") or ""),
        source_seq=int(current.get("source_seq") or 0),
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
            # E7OBS4-04：终态事件附带**合并后的权威快照**——前端不再拿原始阶段字段
            # 自拼状态（刷新与实时必须同一份事实）。
            "progress": dict(final),
            "progress_revision": int(final.get("progress_revision") or 0),
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
    progress: dict[str, Any] | None = None,
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
            # 失败 / 取消终态同样附权威快照（含来源 / 实例 / revision）
            **(
                {
                    "progress": dict(progress),
                    "progress_revision": int(progress.get("progress_revision") or 0),
                }
                if progress
                else {}
            ),
        },
    )

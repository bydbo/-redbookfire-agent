"""对话轮次运行器与 SSE 事件总线的离线单测（S7.4）。

不联网、不连真 Redis、不连库：Redis 换成进程内的假实现（发布 → 订阅真走一遍），
engine / 会话工厂 / 消息写回全部注入替身。覆盖四件事：

1. `sse_frame` 的帧格式（`event:` 头 + 永远只有一行的 `data:`）；
2. `TurnEvents` 的发布 → 订阅往返（顺手证明坏 JSON 被丢掉、通道名带 turn_id）；
3. SSE 生成器的事件顺序、心跳注释与终端事件收尾；
4. detached 的 `start_turn`：整轮跑完把终态写回消息；中途异常则标 `failed` 并发 `error`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from opentelemetry import trace as otel_trace

from xhs_agent.api.routers import chat as chat_router
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import DependencyUnavailableError
from xhs_agent.core.logging import current_turn_id
from xhs_agent.core.tracing import (
    Generation,
    LangfuseTracer,
    NullTracer,
    TracingProvider,
)
from xhs_agent.services import chat_tools, chat_turns
from xhs_agent.services.chat_tools import ToolOutcome
from xhs_agent.services.chat_turns import TurnEvents, redis_url, sse_frame, start_turn
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult
from xhs_agent.workflows.chat import ChatTurn

REDIS_URL = "redis://localhost:6379/0"


# ---------- 假 Redis：进程内 broker，publish 直接投递到订阅者的队列 ----------


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.subscribers: dict[str, list[_PubSub]] = {}

    async def dispatch(self, channel: str, body: Any) -> None:
        for sub in list(self.subscribers.get(channel, [])):
            await sub.queue.put({"type": "message", "channel": channel, "data": body})


class _PubSub:
    def __init__(self, broker: _Broker) -> None:
        self.broker = broker
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._closed = False

    async def subscribe(self, channel: str) -> None:
        self.broker.subscribers.setdefault(channel, []).append(self)

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        while not self._closed:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def unsubscribe(self, channel: str) -> None:
        subs = self.broker.subscribers.get(channel, [])
        if self in subs:
            subs.remove(self)

    async def aclose(self) -> None:
        self._closed = True
        await self.queue.put(None)


class _Client:
    def __init__(self, broker: _Broker) -> None:
        self.broker = broker
        self.closed = False

    async def publish(self, channel: str, body: str) -> int:
        self.broker.published.append((channel, body))
        await self.broker.dispatch(channel, body)
        return len(self.broker.subscribers.get(channel, []))

    def pubsub(self) -> _PubSub:
        return _PubSub(self.broker)

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """假 `redis.asyncio` 模块：`from_url` 出来的客户端共享同一个 broker。"""

    def __init__(self) -> None:
        self.broker = _Broker()
        self.clients: list[_Client] = []

    def from_url(self, _url: str) -> _Client:
        client = _Client(self.broker)
        self.clients.append(client)
        return client


# ---------- 夹具 ----------


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(chat_turns, "aioredis", fake)
    return fake


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    # 本文件只测轮次事件 / 收尾；持久状态与回退保护由真库上下文回归覆盖。
    async def no_persisted_context(*_args: Any, **_kwargs: Any):
        return None
    monkeypatch.setattr(chat_turns.context_state, "load", no_persisted_context)
    return write_config(tmp_path)


class DummySession:
    """假会话：只满足 `async with factory() as session` 的形状（不碰库）。"""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def dummy_factory() -> DummySession:
    return DummySession()


async def no_recent_attachments(_session: Any, _session_id: uuid.UUID) -> list[Any]:
    """假的本会话最近图片查询：这些用例用的是假会话，不碰库（回落由 chat 单测覆盖）。"""
    return []


class DummyEngine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


class ScriptedProvider(llm.BaseProvider):
    """按剧本回一段正文（可真流式），记账口径与生产 provider 一致。"""

    name = "scripted"

    def __init__(self, text: str = "跑好了", deltas: list[str] | None = None) -> None:
        super().__init__(model="scripted-1")
        self.text = text
        self.deltas = list(deltas or [])
        self.calls: list[llm.LLMCall] = []

    async def complete(self, call: llm.LLMCall) -> LLMResult:  # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(
        self, call: llm.LLMCall, on_delta: Any = None
    ) -> LLMResult:
        self.calls.append(call)
        if on_delta is not None:
            for piece in self.deltas:
                await on_delta(piece)
        return LLMResult(
            text=self.text,
            provider=self.name,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=5,
            cost_cny=0.002,
            usage_status="actual",
        )  # R5：假 provider 也按真实契约标来源


async def _collect(events: TurnEvents, sink: list[dict[str, Any]]) -> None:
    async for item in events.stream():
        sink.append(item)


async def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    """轮询等待（事件要经过 drain 任务搬运，不能假设 publish 后立刻可读）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.005)


# ---------- 1. 帧格式与 URL 校验 ----------


def test_sse_frame_keeps_data_on_a_single_line() -> None:
    frame = sse_frame("turn_started", {"session_id": "s-1", "text": "你好\n世界"})

    assert frame.startswith("event: turn_started\ndata: ")
    assert frame.endswith("\n\n")
    body = frame.split("data: ", 1)[1].strip()
    assert "\n" not in body  # 换行必须被 JSON 转义掉，否则 SSE 帧会被拆断
    assert json.loads(body) == {"session_id": "s-1", "text": "你好\n世界"}


def test_redis_url_requires_a_redis_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    cfg = write_config(tmp_path)
    with pytest.raises(DependencyUnavailableError):
        redis_url(cfg)

    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    assert redis_url(write_config(tmp_path)) == REDIS_URL


# ---------- 2. 发布 → 订阅往返 ----------


@pytest.mark.asyncio
async def test_publish_reaches_subscriber(
    cfg: AppConfig, fake_redis: FakeRedis
) -> None:
    events = TurnEvents(cfg, turn_id="t-1")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    try:
        await events.publish("turn_started", {"message_id": "m-1"})
        await events.publish("text_delta", {"text": "你好"})
        await _wait_until(lambda: len(received) >= 2)
    finally:
        pump.cancel()
        await events.aclose()

    assert [item["event"] for item in received] == ["turn_started", "text_delta"]
    assert received[1]["data"] == {"text": "你好"}
    channels = [channel for channel, _body in fake_redis.broker.published]
    assert channels == ["xhs_agent:chat:t-1", "xhs_agent:chat:t-1"]


@pytest.mark.asyncio
async def test_broken_json_is_dropped(cfg: AppConfig, fake_redis: FakeRedis) -> None:
    events = TurnEvents(cfg, turn_id="t-2")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    try:
        await fake_redis.broker.dispatch(events.channel, b"{not json")
        await events.publish("turn_finished", {"status": "succeeded"})
        await _wait_until(lambda: received)
    finally:
        pump.cancel()
        await events.aclose()

    assert [item["event"] for item in received] == ["turn_finished"]


# ---------- 3. SSE 生成器 ----------


@pytest.mark.asyncio
async def test_sse_stream_heartbeats_then_closes_on_terminal_event(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_router, "HEARTBEAT_S", 0.05)
    events = TurnEvents(cfg, turn_id="t-3")
    await events.open()
    frames: list[str] = []

    async def consume() -> None:
        async for frame in chat_router.sse_stream(events):
            frames.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.12)  # 静默够久 → 至少一条心跳
    await events.publish("turn_started", {"message_id": "m-3"})
    await _wait_until(lambda: any("turn_started" in item for item in frames))
    await events.publish("turn_finished", {"status": "succeeded"})
    await asyncio.wait_for(task, timeout=2)

    assert frames and frames[0] == ": ping\n\n"
    assert any(item.startswith("event: turn_started") for item in frames)
    assert frames[-1].startswith("event: turn_finished")  # 终端事件后生成器收尾


# ---------- 4. detached 运行器 ----------


@pytest.mark.asyncio
async def test_start_turn_persists_terminal_message(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(
        chat_turns, "create_session_factory", lambda _engine: dummy_factory
    )
    monkeypatch.setattr(
        chat_turns,
        "build_turn_provider",
        lambda _cfg, _client, tracer=None: ScriptedProvider("跑好了", ["跑", "好了"]),
    )

    events = TurnEvents(cfg, turn_id="t-4")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="帮我蹭一下这个热点",
    )
    assert isinstance(task, asyncio.Task)  # detached：调用方拿到任务就走
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(
            lambda: any(item["event"] == "turn_finished" for item in received)
        )
    finally:
        pump.cancel()
        await events.aclose()

    names = [item["event"] for item in received]
    assert names[0] == "turn_started"
    assert "text_delta" in names
    assert names[-1] == "turn_finished"
    deltas = [
        item["data"]["text"] for item in received if item["event"] == "text_delta"
    ]
    assert deltas == ["跑", "好了"]
    assert saved[-1][0] == message_id
    assert saved[-1][1]["status"] == "succeeded"
    assert saved[-1][1]["content"] == "跑好了"
    assert engine.disposed == 1  # 自建 engine 用完就放


@pytest.mark.asyncio
async def test_start_turn_marks_message_failed_on_crash(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(
        chat_turns, "create_session_factory", lambda _engine: dummy_factory
    )
    monkeypatch.setattr(
        chat_turns,
        "build_turn_provider",
        lambda _cfg, _client, tracer=None: ScriptedProvider(),
    )
    monkeypatch.setattr(chat_turns, "run_turn", boom)

    events = TurnEvents(cfg, turn_id="t-5")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert [item["event"] for item in received] == ["turn_started", "error"]
    assert saved[-1][0] == message_id
    assert saved[-1][1]["status"] == "failed"
    assert "RuntimeError" in saved[-1][1]["error"]
    assert "RuntimeError" in received[-1]["data"]["message"]
    assert "boom" not in json.dumps(saved, default=str)
    assert "boom" not in json.dumps(received)
    assert engine.disposed == 1


class _BrokenSdkClient:
    """整体故障的假 SDK：建观测 / 上报 / flush / 取 trace 全抛（OBS-01 兜底验收）。"""

    def start_as_current_observation(self, **_kwargs: Any) -> Any:
        raise RuntimeError("SDK 建观测失败")

    def update_current_generation(self, **_kwargs: Any) -> None:
        raise RuntimeError("SDK 上报失败")

    def update_current_span(self, **_kwargs: Any) -> None:
        raise RuntimeError("SDK 上报失败")

    def flush(self) -> None:
        raise RuntimeError("SDK flush 失败")

    def get_current_trace_id(self) -> str:
        raise RuntimeError("SDK 取 trace 失败")

    def get_trace_url(self, *, trace_id: str | None = None) -> str:
        raise RuntimeError("SDK 取链接失败")


@pytest.mark.asyncio
async def test_crash_path_survives_whole_sdk_failure(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1（OBS-01）：追踪 SDK 整体故障时，失败兜底照常落库 + 广播，不再崩一次。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(
        chat_turns, "create_session_factory", lambda _engine: dummy_factory
    )
    monkeypatch.setattr(
        chat_turns,
        "build_turn_provider",
        lambda _cfg, _client, tracer=None: ScriptedProvider(),
    )
    monkeypatch.setattr(chat_turns, "run_turn", boom)
    monkeypatch.setattr(
        chat_turns,
        "get_tracer",
        lambda _cfg: LangfuseTracer(cfg, client=_BrokenSdkClient()),
    )

    events = TurnEvents(cfg, turn_id="t-sdk")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert [item["event"] for item in received] == ["turn_started", "error"]
    assert saved[-1][1]["status"] == "failed"
    assert "RuntimeError" in saved[-1][1]["error"]
    assert "boom" not in json.dumps(saved, default=str)
    assert "boom" not in json.dumps(received)
    assert engine.disposed == 1


# ---------- S7.22：取消 / 超时按业务状态契约收尾 ----------


@pytest.mark.asyncio
async def test_start_turn_maps_timeout_to_failed_terminal_state(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时（`TimeoutError`）收尾（R2 / OBS-03）：消息标 `failed`，真实失败阶段终结为 failed。

    设计约定 §5：取消与超时按既有业务状态契约映射，不能永远停在 running——超时是失败
    （`failed` + 类别 `timeout`），取消才是 `interrupted`（类别 `cancelled`）；错误事件带
    真实失败阶段而不是空串。
    """
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def slow_then_boom(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "stage", {"stage": "retrieve", "status": "running", "attempt": 1}
        )
        raise TimeoutError("PRIVATE_MODEL_REQUEST_SENTINEL")

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(
        chat_turns, "create_session_factory", lambda _engine: dummy_factory
    )
    monkeypatch.setattr(
        chat_turns,
        "build_turn_provider",
        lambda _cfg, _client, tracer=None: ScriptedProvider(),
    )
    monkeypatch.setattr(chat_turns, "run_turn", slow_then_boom)

    events = TurnEvents(cfg, turn_id="t-6")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    # 阶段先落 running（可见"在检索"），随后随失败一起收成 interrupted
    running_writes = [
        fields
        for _mid, fields in saved
        if (fields.get("progress") or {}).get("status") == "running"
    ]
    assert running_writes and running_writes[0]["progress"]["stage"] == "retrieve"
    final = saved[-1][1]
    assert final["status"] == "failed" and "超时" in final["error"]
    assert "PRIVATE_MODEL_REQUEST_SENTINEL" not in json.dumps(saved, default=str)
    assert "PRIVATE_MODEL_REQUEST_SENTINEL" not in json.dumps(received)
    assert final["progress"]["status"] == "failed"  # 失败阶段终结为 failed
    assert final["progress"]["stage"] == "retrieve"  # 真实失败阶段，不臆造 final_reply
    assert final["progress"]["error_category"] == "timeout"
    assert final["progress"]["nodes"][0]["status"] == "failed"
    error_event = received[-1]
    assert error_event["event"] == "error"
    assert error_event["data"]["error_category"] == "timeout"
    assert error_event["data"]["stage"] == "retrieve"  # OBS-03：真实失败位置
    assert error_event["data"]["turn_id"] == "t-6"
    assert engine.disposed == 1


# ---------- 5. S7.19：对话 provider 默认套追踪 ----------


def test_build_turn_provider_wraps_with_tracer(
    cfg: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对话层 provider 默认套 `TracingProvider`（S7.19）：追松开时 supervisor 调用有 generation。

    追踪关闭（`NullTracer`）时包装器仍在、行为等价——这样生产路径只有一条。
    """
    tracer = NullTracer()
    monkeypatch.setattr(
        chat_turns, "build_provider", lambda _cfg, client=None: ScriptedProvider()
    )
    provider = chat_turns.build_turn_provider(cfg, client=object(), tracer=tracer)
    assert isinstance(provider, TracingProvider)
    assert provider.tracer is tracer
    assert provider.name == "scripted"


# ---------- 6. S7.20：turn_id 随投递传给 worker ----------


@pytest.mark.asyncio
async def test_enqueue_with_turn_passes_turn_id_to_aware_dispatcher() -> None:
    seen: list[tuple[str, str]] = []

    async def enqueue(job_id: str, *, turn_id: str = "") -> None:
        seen.append((job_id, turn_id))

    wrapped = chat_turns._enqueue_with_turn(enqueue, "turn-9")
    assert wrapped is not None
    await wrapped("job-1")
    assert seen == [("job-1", "turn-9")]


@pytest.mark.asyncio
async def test_enqueue_with_turn_leaves_legacy_dispatcher_untouched() -> None:
    """只收 job_id 的替身原样返回：投递语义不变，不为兼容测试引入静默降级。"""
    seen: list[str] = []

    async def enqueue(job_id: str) -> None:
        seen.append(job_id)

    wrapped = chat_turns._enqueue_with_turn(enqueue, "turn-9")
    assert wrapped is enqueue
    await wrapped("job-1")
    assert seen == ["job-1"]


def test_enqueue_with_turn_without_dispatcher_stays_none() -> None:
    assert chat_turns._enqueue_with_turn(None, "turn-9") is None


# ---------- 7. S7.20：工具 span 与投递的父子关系 ----------


@pytest.mark.asyncio
async def test_tool_runner_opens_tool_span_and_enqueue_uses_it(
    cfg: AppConfig, otel_exporter: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工具调用跑在 `tool.<name>` span 内——投递的 traceparent 就取自它（S7.20 验收口径）。"""
    from xhs_agent.api.deps import CeleryDispatcher

    captured: dict[str, Any] = {}
    holder: dict[str, int] = {}

    class RecordingApp:
        def send_task(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs.get("headers") or {})

    class OtelToolTracer:
        """只实现 `tool()`：用真 OTel span 模拟 Langfuse「当前 span」的语义。"""

        enabled = True

        @contextmanager
        def tool(self, *, name: str, metadata: dict[str, Any]) -> Any:
            with otel_trace.get_tracer("xhs_agent.test").start_as_current_span(
                name
            ) as span:
                holder["span_id"] = span.get_span_context().span_id
                yield Generation(metadata=dict(metadata))

    async def fake_handler(
        ctx: Any,
        _args: dict[str, Any],
        *,
        attachments: Any = None,
        on_progress: Any = None,
    ) -> ToolOutcome:
        await ctx.enqueue("job-1")  # 投递发生在工具 span 内
        return ToolOutcome(status="succeeded")

    monkeypatch.setitem(chat_tools.HANDLERS, chat_tools.TOOL_RUN_ANALYSIS, fake_handler)
    dispatcher = CeleryDispatcher(cfg)
    dispatcher._app = RecordingApp()  # type: ignore[assignment]

    runner = chat_turns.build_tool_runner(
        cast(Any, None),
        cfg=cfg,
        client=object(),
        session_id=uuid.uuid4(),
        attachments=[],
        dispatcher=dispatcher,
        turn_id="turn-9",
        tracer=OtelToolTracer(),
    )
    outcome = await runner(chat_tools.TOOL_RUN_ANALYSIS, {})

    assert outcome.status == "succeeded"
    assert captured["x-xhs-turn-id"] == "turn-9"
    assert format(holder["span_id"], "016x") in str(captured["traceparent"])


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_tool_structure_failure_or_cancel_keeps_prior_repair_usage(monkeypatch, cancel):
    from types import SimpleNamespace

    from xhs_agent.core.ledger import project
    from xhs_agent.services import chat_tools
    from xhs_agent.services.chat_tools import ToolOutcome

    caller = SimpleNamespace(records=[])
    ledger = {"records": [], "calls": {"scope1": {"tool": "fixture", "scope": "scope1", "call_id": "scope1"}}}

    async def tool(ctx, _args, **_kwargs):
        ctx.caller.records.append({"task": "copy_revise", "cost_cny": 0.01, "prompt_tokens": 100,
                                   "completion_tokens": 20, "usage_status": "actual"})
        if cancel:
            raise asyncio.CancelledError()
        return ToolOutcome(status="failed", payload={"usage_status": "unknown"}, error="bad schema")

    monkeypatch.setitem(chat_tools.HANDLERS, "fixture", tool)
    runner = chat_turns.build_tool_runner(None, cfg=AppConfig(), client=None, session_id=uuid.uuid4(),
        attachments=[], dispatcher=None, caller=caller, ledger=ledger)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await runner("fixture", {}, meta={"scope": "scope1"})
        total = project(ledger)
        assert total["prompt_tokens"] == 100 and total["cost_cny"] == 0.01
        assert total["prompt_versions"] == {"copy_revise": 3}
    else:
        outcome = await runner("fixture", {}, meta={"scope": "scope1"})
        assert outcome.payload["prompt_tokens"] == 100 and outcome.payload["cost_cny"] == 0.01
        assert outcome.prompt_versions == {"copy_revise": 3}


# ---------- R2（OBS-03 / 04 / 09）：一个入口、真实失败阶段、取消终态 ----------


class _CountingTracer(NullTracer):
    """数业务入口 + 固定 trace id：OBS-04 验收用（同一轮只允许一个 `chat.turn`）。"""

    def __init__(self) -> None:
        self.turns = 0

    def turn(self, *, name: str, metadata: Any, turn_id: str = ""):  # type: ignore[override]
        self.turns += 1

        @contextmanager
        def _noop():
            yield None

        return _noop()

    def current_trace_id(self) -> str:
        return "trace-fixed-1"


class _FlakyFactory:
    """第一次可用、之后连库都打不开：验证收尾写库失败的兜底（OBS-04 点 4）。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("数据库不可用")
        return self._inner()


def _patch_turn_harness(
    monkeypatch: pytest.MonkeyPatch, engine: DummyEngine, update: Any, tracer: Any
) -> None:
    monkeypatch.setattr(chat_turns, "update_message", update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(
        chat_turns, "create_session_factory", lambda _engine: dummy_factory
    )
    monkeypatch.setattr(
        chat_turns,
        "build_turn_provider",
        lambda _cfg, _client, tracer=None: ScriptedProvider(),
    )
    monkeypatch.setattr(chat_turns, "get_tracer", lambda _cfg: tracer)


@pytest.mark.asyncio
async def test_cancelled_turn_finalizes_interrupted_and_propagates(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBS-09：真实 `Task.cancel()` → 消息 `interrupted`（类别 cancelled）→ 取消继续传播。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    started = asyncio.Event()

    async def hanging(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "stage", {"stage": "supervisor", "status": "running", "attempt": 1}
        )
        started.set()
        await asyncio.sleep(3600)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", hanging)

    events = TurnEvents(cfg, turn_id="t-cancel")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)  # 有界：收尾后立即传播取消
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    final = saved[-1][1]
    assert final["status"] == "interrupted"
    assert final["progress"]["status"] == "interrupted"
    assert final["progress"]["stage"] == "supervisor"
    assert final["progress"]["error_category"] == "cancelled"
    error_event = received[-1]
    assert error_event["event"] == "error"
    assert error_event["data"]["error_category"] == "cancelled"
    assert error_event["data"]["stage"] == "supervisor"
    assert error_event["data"]["turn_id"] == "t-cancel"
    assert engine.disposed == 1
    assert current_turn_id() == ""  # 日志上下文复位


@pytest.mark.asyncio
async def test_cancelled_turn_keeps_completed_ledger_entries(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5（E7OBS2-01）：取消时稳定边界账本里**已完成**的工具 / 账目 / 引用尽力写回。

    in-flight 的调用拿不到、如实缺席——不承诺"都已保存"，但已完成的不能丢。
    """
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: dict[str, Any]
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    run_uuid = "77777777-7777-4777-8777-777777777777"
    started = asyncio.Event()

    async def partial_then_hang(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        ledger = kwargs.get("ledger")
        assert ledger is not None  # 运行器注入同一份账本
        ledger["records"].append(
            {
                "task": "chat_supervisor",
                "cost_cny": 0.001,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "usage_status": "actual",
            }
        )
        ledger["tool_calls"].append(
            {
                "tool": "run_hotspot_analysis",
                "status": "succeeded",
                "call_id": "c1",
                "attempt": 1,
                "run_id": run_uuid,
                "cost_cny": 0.05,
                "prompt_tokens": 700,
                "completion_tokens": 300,
                "latency_ms": 1200,
                "error": "",
                "steps": [],
                "usage_status": "actual",
            }
        )
        ledger["run_id"] = run_uuid
        ledger["versions"]["run_hotspot_analysis"] = 1
        await on_event(
            "stage", {"stage": "supervisor", "status": "running", "attempt": 2}
        )
        started.set()
        await asyncio.sleep(3600)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", partial_then_hang)

    events = TurnEvents(cfg, turn_id="t-cancel-ledger")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        pump.cancel()
        await events.aclose()

    final = saved[-1][1]
    assert final["status"] == "interrupted"
    assert final["run_id"] == run_uuid  # 运行引用保留
    assert [item["tool"] for item in final["tool_calls"]] == ["run_hotspot_analysis"]
    assert final["cost_cny"] == pytest.approx(0.051)  # 已知消费保留
    assert final["prompt_tokens"] == 710 and final["completion_tokens"] == 305
    assert final["prompt_versions"] == {"run_hotspot_analysis": 1}


def test_terminal_snapshot_keeps_historical_failure_and_interrupts_current() -> None:
    """R5（E7OBS2-07）：取消只把**当前执行**投影成 interrupted/cancelled；
    之前失败的尝试保留在 nodes 里，不被统一收尾改写。"""
    from xhs_agent.core import progress as progress_tool

    snapshot = progress_tool.begin(None, stage="supervisor", attempt=1)
    snapshot = progress_tool.end(
        snapshot,
        stage="supervisor",
        status="failed",
        attempt=1,
        error_category="llm_error",
    )
    snapshot = progress_tool.begin(snapshot, stage="supervisor", attempt=2)

    final = chat_turns._terminal_snapshot(
        snapshot, status="interrupted", error_category="cancelled"
    )

    assert final["stage"] == "supervisor" and final["status"] == "interrupted"
    assert final["attempt"] == 2 and final["error_category"] == "cancelled"
    nodes = {
        (node["stage"], node["attempt"]): node["status"] for node in final["nodes"]
    }
    assert nodes[("supervisor", 1)] == "failed"  # 历史失败保留
    assert nodes[("supervisor", 2)] == "interrupted"


@pytest.mark.asyncio
async def test_second_cancel_during_finalize_still_propagates(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5（E7OBS2-07）：收尾写库被卡住时再取消——取消继续传播，绝不吞掉控制流；
    尽力边界如实（那一笔写不进去由陈旧对账兜底）。"""
    write_started = asyncio.Event()

    async def slow_update(
        _session: Any, _message_id: uuid.UUID, **fields: dict[str, Any]
    ) -> dict[str, Any]:
        # 只卡住**收尾**写库（带 status 的终态写回）；阶段快照写库照常放行
        if fields.get("status") == "interrupted":
            write_started.set()
            await asyncio.sleep(3600)  # 卡住收尾写库
        return {}

    started = asyncio.Event()

    async def hanging(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "stage", {"stage": "supervisor", "status": "running", "attempt": 1}
        )
        started.set()
        await asyncio.sleep(3600)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, slow_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", hanging)

    events = TurnEvents(cfg, turn_id="t-cancel-twice")
    await events.open()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(write_started.wait(), timeout=5)
        task.cancel()  # 第二次取消：收尾还在写
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        await events.aclose()
    assert engine.disposed == 1  # 资源照常释放


@pytest.mark.asyncio
async def test_single_turn_entry_and_trace_id_persisted_on_crash(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBS-04：异常路径只有一个业务入口；trace_id 进入 turn 后尽早捕获并落库。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    engine = DummyEngine()
    tracer = _CountingTracer()
    _patch_turn_harness(monkeypatch, engine, fake_update, tracer)
    monkeypatch.setattr(chat_turns, "run_turn", boom)

    events = TurnEvents(cfg, turn_id="t-one")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert tracer.turns == 1  # 不重建第二个 chat.turn
    assert [item["event"] for item in received] == ["turn_started", "error"]
    assert saved[0][1]["trace_id"] == "trace-fixed-1"  # 尽早落库
    assert saved[-1][1]["trace_id"] == "trace-fixed-1"
    assert received[-1]["data"]["trace_id"] == "trace-fixed-1"


@pytest.mark.asyncio
async def test_failed_turn_keeps_real_stage_and_never_fakes_final_reply(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OBS-03：返回失败不做 final_reply；终态快照与事件停在真实失败阶段。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def failing(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "stage", {"stage": "supervisor", "status": "running", "attempt": 1}
        )
        await on_event(
            "stage",
            {
                "stage": "supervisor",
                "status": "failed",
                "attempt": 1,
                "error_category": "llm_error",
            },
        )
        return ChatTurn(
            status="failed",
            error="模型拒绝了",
            error_category="llm_error",
            latency_ms=12,
        )

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", failing)

    events = TurnEvents(cfg, turn_id="t-fail")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    stage_events = [item["data"] for item in received if item["event"] == "stage"]
    assert all(
        item["stage"] != "final_reply" for item in stage_events
    )  # 不臆造收尾阶段
    final = saved[-1][1]
    assert final["status"] == "failed" and final["latency_ms"] == 12
    assert final["progress"]["stage"] == "supervisor"
    assert final["progress"]["status"] == "failed"
    assert final["progress"]["error_category"] == "llm_error"
    error_event = received[-1]
    assert error_event["data"]["stage"] == "supervisor"
    assert error_event["data"]["error_category"] == "llm_error"


@pytest.mark.asyncio
async def test_finalize_write_failure_is_logged_not_claimed(
    cfg: AppConfig,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OBS-04 点 4：收尾写库失败 → 明确记录"未保存"，事件照常广播，不谎称已落库。"""

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        return {}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", boom)
    flaky = _FlakyFactory(dummy_factory)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: flaky)

    events = TurnEvents(cfg, turn_id="t-flaky")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        with caplog.at_level(logging.WARNING, logger="xhs_agent.chat"):
            await asyncio.wait_for(task, timeout=5)
            await _wait_until(
                lambda: any(item["event"] == "error" for item in received)
            )
    finally:
        pump.cancel()
        await events.aclose()

    assert any("轮次收尾写库失败" in item.getMessage() for item in caplog.records)
    assert received[-1]["event"] == "error"


@pytest.mark.asyncio
async def test_worker_stage_updates_persist_real_status_retries_and_identity(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3（OBS-05）：worker 的 failed / 重试 / skipped / run 身份都进快照，不再一律 running。

    失败尝试仍可追查（`nodes` 保留 clue/attempt=1/failed），重试成功不抹掉失败记录。
    """
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def structured_run(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "stage", {"stage": "submit_analysis", "status": "running", "attempt": 1}
        )
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "拆解",
                "stage_key": "clue",
                "status": "failed",
                "attempt": 1,
                "retries": 2,
                "error_category": "llm_error",
                "run_id": "run-9",
                "job_id": "job-9",
            },
        )
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "拆解",
                "stage_key": "clue",
                "status": "running",
                "attempt": 2,
                "retries": 2,
                "error_category": "",
                "run_id": "run-9",
                "job_id": "job-9",
            },
        )
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "报告",
                "stage_key": "report",
                "status": "succeeded",
                "attempt": 1,
                "retries": 0,
                "error_category": "",
                "run_id": "run-9",
                "job_id": "job-9",
                "latency_ms": 7,
            },
        )
        return ChatTurn(status="succeeded", reply="好了", run_id="run-9", cost_cny=0.05)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", structured_run)

    events = TurnEvents(cfg, turn_id="t-worker")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=message_id,
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        pump.cancel()
        await events.aclose()

    final = saved[-1][1]
    assert final["status"] == "succeeded" and final["run_id"] == "run-9"
    snapshot = final["progress"]
    assert snapshot["stage"] == "final_reply" and snapshot["status"] == "succeeded"
    assert snapshot["last_finished_stage"] == "final_reply"
    assert snapshot["retries"] == 0  # 当前阶段（报告）的投递重试数
    nodes = {
        (item["stage"], item["attempt"], item.get("run_id") or ""): item
        for item in snapshot["nodes"]
    }
    assert nodes[("clue", 1, "run-9")]["status"] == "failed"  # 失败尝试可追查
    assert nodes[("clue", 1, "run-9")]["error_category"] == "llm_error"
    assert nodes[("report", 1, "run-9")]["latency_ms"] == 7
    # E7OBS4-03：不再"切阶段即自动成功"——没有明确终态的阶段不补造节点。
    # clue attempt=2 只上报过 running，report 到来时不能被追认成功；
    # submit_analysis 同理（工具只报了 running），保持"未结束 / 未知"。
    assert ("clue", 2, "run-9") not in nodes
    assert all(item["stage"] != "submit_analysis" for item in snapshot["nodes"])


@pytest.mark.asyncio
async def test_late_cross_run_terminal_never_regresses_persisted_snapshot(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5（E7OBS2-02）：迟到旧 run 终态只补 nodes——顶层快照不回退、不串身份，
    也不触发"切阶段即成功"的自动收尾。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: dict[str, Any]
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    run_new = "11111111-1111-4111-8111-111111111111"
    run_old = "22222222-2222-4222-8222-222222222222"

    async def late_run(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "报告",
                "stage_key": "report",
                "status": "running",
                "attempt": 1,
                "run_id": run_new,
                "job_id": "job-1",
            },
        )
        # 旧 run 的迟到终态（跨 stage，且尝试号更高）：不许回退、不许串身份
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "撰稿",
                "stage_key": "draft",
                "status": "succeeded",
                "attempt": 3,
                "run_id": run_old,
                "job_id": "job-old",
            },
        )
        # 同 stage、旧 run 的"冲突终态"：同样不改写当前状态
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "报告",
                "stage_key": "report",
                "status": "failed",
                "attempt": 9,
                "error_category": "llm_error",
                "run_id": run_old,
                "job_id": "job-old",
            },
        )
        return ChatTurn(status="succeeded", reply="好了", run_id=run_new, cost_cny=0.05)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", late_run)

    events = TurnEvents(cfg, turn_id="t-late")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        pump.cancel()
        await events.aclose()

    # 所有被持久化的快照里，顶层从未回到 draft；迟到终态期间身份保持 run-new
    snapshots = [fields["progress"] for _mid, fields in saved if "progress" in fields]
    assert snapshots and all(item["stage"] != "draft" for item in snapshots)
    late_snapshot = next(
        item
        for item in snapshots
        if any(node.get("run_id") == run_old for node in item["nodes"])
    )
    assert late_snapshot["stage"] == "report" and late_snapshot["status"] == "running"
    assert late_snapshot["run_id"] == run_new
    # 旧 run 的历史事实补进了 nodes；report 没有被迟到事件提前收尾
    late_nodes = {
        (node["stage"], node.get("run_id") or ""): node["status"]
        for node in late_snapshot["nodes"]
    }
    assert late_nodes[("draft", run_old)] == "succeeded"
    assert ("report", run_new) not in late_nodes


@pytest.mark.asyncio
async def test_tool_finished_never_overrides_worker_attempt_or_status(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5（E7OBS2-03）：worker 阶段的结构化终态由 tool_progress 负责；tool_finished
    的"工具第几次调用"不得覆盖阶段重试号，也不得在 worker 仍 running 时凭空收尾。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: dict[str, Any]
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    run_new = "33333333-3333-4333-8333-333333333333"

    async def structured_run(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "报告",
                "stage_key": "report",
                "status": "running",
                "attempt": 1,
                "retries": 2,
                "run_id": run_new,
                "job_id": "job-1",
            },
        )
        # 工具成功、worker 最后一份快照仍 running（如超时返回）：不许收尾、不许改重试号
        await on_event(
            "tool_finished",
            {
                "tool": "run_hotspot_analysis",
                "stage": "报告",
                "stage_key": "report",
                "status": "succeeded",
                "attempt": 1,
                "structured": True,
                "worker_status": "running",
                "worker_attempt": 1,
                "worker_retries": 2,
                "run_id": run_new,
            },
        )
        return ChatTurn(status="succeeded", reply="好了", run_id=run_new, cost_cny=0.05)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", structured_run)

    events = TurnEvents(cfg, turn_id="t-tf")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        pump.cancel()
        await events.aclose()

    snapshots = [fields["progress"] for _mid, fields in saved if "progress" in fields]
    report_snapshots = [item for item in snapshots if item["stage"] == "report"]
    assert report_snapshots  # 确实停在 report 上
    assert all(item["status"] == "running" for item in report_snapshots)
    assert all(item["retries"] == 2 for item in report_snapshots)  # 未被工具次数清零
    assert all(
        not any(node["stage"] == "report" for node in item["nodes"])
        for item in report_snapshots
    )  # 未凭空收尾


@pytest.mark.asyncio
async def test_tool_finished_uses_worker_status_for_structured_stages(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结构化阶段的收尾用 worker 真实状态（skipped 不冒充 succeeded）、worker 重试号。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: dict[str, Any]
    ) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    run_new = "44444444-4444-4444-8444-444444444444"

    async def skipped_run(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event(
            "tool_progress",
            {
                "tool": "run_hotspot_analysis",
                "stage": "拆解",
                "stage_key": "clue",
                "status": "running",
                "attempt": 1,
                "retries": 0,
                "run_id": run_new,
                "job_id": "job-1",
            },
        )
        await on_event(
            "tool_finished",
            {
                "tool": "run_hotspot_analysis",
                "stage": "拆解",
                "stage_key": "clue",
                "status": "succeeded",
                "attempt": 1,
                "structured": True,
                "worker_status": "skipped",
                "worker_attempt": 1,
                "worker_retries": 0,
                "latency_ms": 0,
                "run_id": run_new,
            },
        )
        return ChatTurn(status="succeeded", reply="好了", run_id=run_new)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", skipped_run)

    events = TurnEvents(cfg, turn_id="t-tf-skip")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=5)
    finally:
        pump.cancel()
        await events.aclose()

    final = saved[-1][1]["progress"]
    nodes = {
        (node["stage"], node.get("run_id") or ""): node["status"]
        for node in final["nodes"]
    }
    assert nodes[("clue", run_new)] == "skipped"  # skip 不虚构成功


# ---------- 9. 部分工具记录可读、可关联（E7OBS4-01 / A1、A2） ----------


class ToolCallingProvider(llm.BaseProvider):
    """第一轮要求调 `run_hotspot_analysis`，之后给正文（这里的用例大多在第一轮就被取消）。"""

    name = "tool-calling"

    def __init__(self, tool: str = "run_hotspot_analysis") -> None:
        super().__init__(model="scripted-1")
        self.tool = tool
        self.rounds = 0

    async def complete(self, call: llm.LLMCall) -> LLMResult:  # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(
        self, call: llm.LLMCall, on_delta: Any = None
    ) -> LLMResult:
        self.rounds += 1
        calls = (
            [
                {
                    "id": "call-abc",
                    "name": self.tool,
                    "arguments": json.dumps({"hotspot": "某条热点", "topk": 3}),
                }
            ]
            if self.rounds == 1
            else []
        )
        return LLMResult(
            text="" if calls else "已经跑完了",
            provider=self.name,
            model=self.model,
            prompt_tokens=10,
            completion_tokens=5,
            cost_cny=0.001,
            usage_status="actual",
            tool_calls=calls,
        )


def _patch_turn_provider(
    monkeypatch: pytest.MonkeyPatch, provider: llm.BaseProvider
) -> None:
    monkeypatch.setattr(
        chat_turns, "build_turn_provider", lambda _cfg, _client, tracer=None: provider
    )


@pytest.mark.asyncio
async def test_partial_tool_record_keeps_real_identity_after_cancel(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1：图片成功、提交成功、等待时取消 —— 落库的部分记录保留真实身份且能通过契约校验。

    这里用**真实对话图 + 真实工具执行器**，只把工具处理函数换成"上报两步后挂住"的替身，
    取消用真实 `Task.cancel()`；供应商等待被完全替身化（不联网、不等待真实超时）。
    """
    saved: list[dict[str, Any]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append(fields)
        return {}

    started = asyncio.Event()

    async def hanging_tool(
        _ctx: Any, _args: dict[str, Any], *, attachments: Any = None, on_progress: Any = None
    ) -> ToolOutcome:
        if on_progress is not None:
            await on_progress("解析图片", 0, 0)
            await on_progress("提交分析", 0, 0)
        started.set()
        await asyncio.sleep(3600)
        return ToolOutcome(status="succeeded")  # pragma: no cover - 不会走到

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    _patch_turn_provider(monkeypatch, ToolCallingProvider())
    monkeypatch.setitem(chat_tools.HANDLERS, "run_hotspot_analysis", hanging_tool)

    events = TurnEvents(cfg, turn_id="t-partial")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="只发图，跑一下",
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    final = saved[-1]
    assert final["status"] == "interrupted"
    records = final["tool_calls"]
    assert len(records) == 1
    record = records[0]
    # 真实调用身份：调用开始时就登记，不需要等工具结束
    assert record["tool"] == "run_hotspot_analysis"
    assert record["call_id"] == "call-abc" and record["attempt"] == 1
    assert record["args"] == {"hotspot": "某条热点", "topk": 3}   # 不虚构、不丢失参数
    assert record["status"] == "interrupted"                      # 取消 ≠ 普通失败
    assert record["run_id"] is None                               # 没提交过：空值，不编 uuid
    assert [step["stage"] for step in record["steps"]] == ["解析图片", "提交分析"]
    # 契约：这条记录能通过公开响应模型（旧实现在这里 500）
    from xhs_agent.api.models import ChatToolCall

    model = ChatToolCall.model_validate(record)
    assert model.args["hotspot"] == "某条热点"
    assert model.status == "interrupted"


@pytest.mark.asyncio
async def test_ordinary_tool_exception_keeps_args_and_marks_failed(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2：工具普通异常 —— 参数与身份仍可读，状态 failed，不伪造工具成功。"""
    saved: list[dict[str, Any]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append(fields)
        return {}

    async def boom(
        _ctx: Any, _args: dict[str, Any], *, attachments: Any = None, on_progress: Any = None
    ) -> ToolOutcome:
        if on_progress is not None:
            await on_progress("解析图片", 0, 0)
        raise RuntimeError("上游炸了")

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    _patch_turn_provider(monkeypatch, ToolCallingProvider())
    monkeypatch.setitem(chat_tools.HANDLERS, "run_hotspot_analysis", boom)

    events = TurnEvents(cfg, turn_id="t-boom")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=10)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    final = saved[-1]
    assert final["status"] == "failed"
    record = final["tool_calls"][0]
    assert record["status"] == "failed"
    assert record["args"] == {"hotspot": "某条热点", "topk": 3}
    assert record["call_id"] == "call-abc"
    assert record["run_id"] is None
    assert final["progress"]["status"] == "failed"   # 阶段如实失败，不伪造成功
    assert "error" in final


@pytest.mark.asyncio
async def test_persist_adjudicates_before_update_so_late_source_event_cannot_regress(
    cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1（persist 层）：report/running/seq=5 之后同源 draft/succeeded/seq=3 —— report 仍 running。

    旧实现在合并前就把"上一阶段"推断成 succeeded（并把来源退回 run_id），迟到的旧序号事件
    因此绕过序号比较；这里钉住"只补历史、不动当前"。
    """
    saved: list[dict[str, Any]] = []

    async def fake_update(
        _session: Any, message_id: uuid.UUID, **fields: Any
    ) -> dict[str, Any]:
        saved.append(fields)
        return {}

    async def scripted(*_args: Any, **kwargs: Any) -> ChatTurn:
        on_event = kwargs["on_event"]
        await on_event("tool_progress", {
            "tool": "run_hotspot_analysis", "stage_key": "report", "stage": "报告",
            "status": "running", "attempt": 1, "run_id": "77777777-7777-4777-8777-777777777777",
            "source_id": "worker:run-a:g0", "stage_instance_id": "worker:run-a:g0:report:1",
            "source_seq": 5,
        })
        await on_event("tool_progress", {
            "tool": "run_hotspot_analysis", "stage_key": "draft", "stage": "撰稿",
            "status": "succeeded", "attempt": 1, "run_id": "77777777-7777-4777-8777-777777777777",
            "source_id": "worker:run-a:g0", "stage_instance_id": "worker:run-a:g0:draft:1",
            "source_seq": 3,
        })
        return ChatTurn(reply="跑完了", status="failed", error="模拟失败")

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", scripted)

    events = TurnEvents(cfg, turn_id="t-c1")
    await events.open()
    pump = asyncio.create_task(_collect(events, []))
    task = start_turn(
        cfg,
        events,
        session_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        history=[],
        user_text="跑一下",
    )
    try:
        await asyncio.wait_for(task, timeout=10)
    finally:
        pump.cancel()
        await events.aclose()

    snapshots = [item["progress"] for item in saved if "progress" in item]
    after_late = next(
        item for item in snapshots
        if any(node["stage"] == "draft" and node["status"] == "succeeded"
               for node in item["nodes"])
    )
    assert (after_late["stage"], after_late["status"]) == ("report", "running")
    assert after_late["source_id"] == "worker:run-a:g0"
    assert after_late["source_seq"] == 5                       # 序号不回退
    assert after_late["run_id"] == "77777777-7777-4777-8777-777777777777"
    # 没有任何伪造的 report 成功节点
    assert not any(
        node["stage"] == "report" and node["status"] == "succeeded"
        for node in after_late["nodes"]
    )

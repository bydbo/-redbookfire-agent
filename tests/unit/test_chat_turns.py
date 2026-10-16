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
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
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

    async def complete(self, call: llm.LLMCall) -> LLMResult:      # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(self, call: llm.LLMCall,
                                  on_delta: Any = None) -> LLMResult:
        self.calls.append(call)
        if on_delta is not None:
            for piece in self.deltas:
                await on_delta(piece)
        return LLMResult(text=self.text, provider=self.name, model=self.model,
                         prompt_tokens=10, completion_tokens=5, cost_cny=0.002)


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
    assert "\n" not in body                    # 换行必须被 JSON 转义掉，否则 SSE 帧会被拆断
    assert json.loads(body) == {"session_id": "s-1", "text": "你好\n世界"}


def test_redis_url_requires_a_redis_dsn(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    cfg = write_config(tmp_path)
    with pytest.raises(DependencyUnavailableError):
        redis_url(cfg)

    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    assert redis_url(write_config(tmp_path)) == REDIS_URL


# ---------- 2. 发布 → 订阅往返 ----------


@pytest.mark.asyncio
async def test_publish_reaches_subscriber(cfg: AppConfig, fake_redis: FakeRedis) -> None:
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
        cfg: AppConfig, fake_redis: FakeRedis,
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_router, "HEARTBEAT_S", 0.05)
    events = TurnEvents(cfg, turn_id="t-3")
    await events.open()
    frames: list[str] = []

    async def consume() -> None:
        async for frame in chat_router.sse_stream(events):
            frames.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.12)                  # 静默够久 → 至少一条心跳
    await events.publish("turn_started", {"message_id": "m-3"})
    await _wait_until(lambda: any("turn_started" in item for item in frames))
    await events.publish("turn_finished", {"status": "succeeded"})
    await asyncio.wait_for(task, timeout=2)

    assert frames and frames[0] == ": ping\n\n"
    assert any(item.startswith("event: turn_started") for item in frames)
    assert frames[-1].startswith("event: turn_finished")   # 终端事件后生成器收尾


# ---------- 4. detached 运行器 ----------


@pytest.mark.asyncio
async def test_start_turn_persists_terminal_message(cfg: AppConfig, fake_redis: FakeRedis,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client, tracer=None: ScriptedProvider("跑好了", ["跑", "好了"]))

    events = TurnEvents(cfg, turn_id="t-4")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="帮我蹭一下这个热点")
    assert isinstance(task, asyncio.Task)      # detached：调用方拿到任务就走
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "turn_finished" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    names = [item["event"] for item in received]
    assert names[0] == "turn_started"
    assert "text_delta" in names
    assert names[-1] == "turn_finished"
    deltas = [item["data"]["text"] for item in received if item["event"] == "text_delta"]
    assert deltas == ["跑", "好了"]
    assert saved[-1][0] == message_id
    assert saved[-1][1]["status"] == "succeeded"
    assert saved[-1][1]["content"] == "跑好了"
    assert engine.disposed == 1                # 自建 engine 用完就放


@pytest.mark.asyncio
async def test_start_turn_marks_message_failed_on_crash(
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client, tracer=None: ScriptedProvider())
    monkeypatch.setattr(chat_turns, "run_turn", boom)

    events = TurnEvents(cfg, turn_id="t-5")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert [item["event"] for item in received] == ["turn_started", "error"]
    assert saved[-1][0] == message_id
    assert saved[-1][1]["status"] == "failed"
    assert "boom" in saved[-1][1]["error"]
    assert "boom" in received[-1]["data"]["message"]
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
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """R1（OBS-01）：追踪 SDK 整体故障时，失败兜底照常落库 + 广播，不再崩一次。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client, tracer=None: ScriptedProvider())
    monkeypatch.setattr(chat_turns, "run_turn", boom)
    monkeypatch.setattr(chat_turns, "get_tracer",
                        lambda _cfg: LangfuseTracer(cfg, client=_BrokenSdkClient()))

    events = TurnEvents(cfg, turn_id="t-sdk")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert [item["event"] for item in received] == ["turn_started", "error"]
    assert saved[-1][1]["status"] == "failed"
    assert "boom" in saved[-1][1]["error"]
    assert engine.disposed == 1





# ---------- S7.22：取消 / 超时按业务状态契约收尾 ----------


@pytest.mark.asyncio
async def test_start_turn_maps_timeout_to_failed_terminal_state(
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时（`TimeoutError`）收尾（R2 / OBS-03）：消息标 `failed`，真实失败阶段终结为 failed。

    设计约定 §5：取消与超时按既有业务状态契约映射，不能永远停在 running——超时是失败
    （`failed` + 类别 `timeout`），取消才是 `interrupted`（类别 `cancelled`）；错误事件带
    真实失败阶段而不是空串。
    """
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def slow_then_boom(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event("stage", {"stage": "retrieve", "status": "running", "attempt": 1})
        raise TimeoutError("模型超时")

    engine = DummyEngine()
    monkeypatch.setattr(chat_turns, "update_message", fake_update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client, tracer=None: ScriptedProvider())
    monkeypatch.setattr(chat_turns, "run_turn", slow_then_boom)

    events = TurnEvents(cfg, turn_id="t-6")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    # 阶段先落 running（可见"在检索"），随后随失败一起收成 interrupted
    running_writes = [fields for _mid, fields in saved
                      if (fields.get("progress") or {}).get("status") == "running"]
    assert running_writes and running_writes[0]["progress"]["stage"] == "retrieve"
    final = saved[-1][1]
    assert final["status"] == "failed" and "模型超时" in final["error"]
    assert final["progress"]["status"] == "failed"          # 失败阶段终结为 failed
    assert final["progress"]["stage"] == "retrieve"         # 真实失败阶段，不臆造 final_reply
    assert final["progress"]["error_category"] == "timeout"
    assert final["progress"]["nodes"][0]["status"] == "failed"
    error_event = received[-1]
    assert error_event["event"] == "error"
    assert error_event["data"]["error_category"] == "timeout"
    assert error_event["data"]["stage"] == "retrieve"       # OBS-03：真实失败位置
    assert error_event["data"]["turn_id"] == "t-6"
    assert engine.disposed == 1


# ---------- 5. S7.19：对话 provider 默认套追踪 ----------


def test_build_turn_provider_wraps_with_tracer(
        cfg: AppConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """对话层 provider 默认套 `TracingProvider`（S7.19）：追松开时 supervisor 调用有 generation。

    追踪关闭（`NullTracer`）时包装器仍在、行为等价——这样生产路径只有一条。
    """
    tracer = NullTracer()
    monkeypatch.setattr(chat_turns, "build_provider",
                        lambda _cfg, client=None: ScriptedProvider())
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
        cfg: AppConfig, otel_exporter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
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
            with otel_trace.get_tracer("xhs_agent.test").start_as_current_span(name) as span:
                holder["span_id"] = span.get_span_context().span_id
                yield Generation(metadata=dict(metadata))

    async def fake_handler(ctx: Any, _args: dict[str, Any], *,
                           attachments: Any = None, on_progress: Any = None) -> ToolOutcome:
        await ctx.enqueue("job-1")             # 投递发生在工具 span 内
        return ToolOutcome(status="succeeded")

    monkeypatch.setitem(chat_tools.HANDLERS, chat_tools.TOOL_RUN_ANALYSIS, fake_handler)
    dispatcher = CeleryDispatcher(cfg)
    dispatcher._app = RecordingApp()           # type: ignore[assignment]

    runner = chat_turns.build_tool_runner(
        cast(Any, None), cfg=cfg, client=object(), session_id=uuid.uuid4(),
        attachments=[], dispatcher=dispatcher, turn_id="turn-9",
        tracer=OtelToolTracer())
    outcome = await runner(chat_tools.TOOL_RUN_ANALYSIS, {})

    assert outcome.status == "succeeded"
    assert captured["x-xhs-turn-id"] == "turn-9"
    assert format(holder["span_id"], "016x") in str(captured["traceparent"])


# ---------- R2（OBS-03 / 04 / 09）：一个入口、真实失败阶段、取消终态 ----------


class _CountingTracer(NullTracer):
    """数业务入口 + 固定 trace id：OBS-04 验收用（同一轮只允许一个 `chat.turn`）。"""

    def __init__(self) -> None:
        self.turns = 0

    def turn(self, *, name: str, metadata: Any, turn_id: str = ""):   # type: ignore[override]
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


def _patch_turn_harness(monkeypatch: pytest.MonkeyPatch, engine: DummyEngine,
                        update: Any, tracer: Any) -> None:
    monkeypatch.setattr(chat_turns, "update_message", update)
    monkeypatch.setattr(chat_turns, "latest_attachments", no_recent_attachments)
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client, tracer=None: ScriptedProvider())
    monkeypatch.setattr(chat_turns, "get_tracer", lambda _cfg: tracer)


@pytest.mark.asyncio
async def test_cancelled_turn_finalizes_interrupted_and_propagates(
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """OBS-09：真实 `Task.cancel()` → 消息 `interrupted`（类别 cancelled）→ 取消继续传播。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    started = asyncio.Event()

    async def hanging(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event("stage", {"stage": "supervisor", "status": "running", "attempt": 1})
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
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)          # 有界：收尾后立即传播取消
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
    assert current_turn_id() == ""                           # 日志上下文复位


@pytest.mark.asyncio
async def test_single_turn_entry_and_trace_id_persisted_on_crash(
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """OBS-04：异常路径只有一个业务入口；trace_id 进入 turn 后尽早捕获并落库。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
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
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert tracer.turns == 1                                 # 不重建第二个 chat.turn
    assert [item["event"] for item in received] == ["turn_started", "error"]
    assert saved[0][1]["trace_id"] == "trace-fixed-1"        # 尽早落库
    assert saved[-1][1]["trace_id"] == "trace-fixed-1"
    assert received[-1]["data"]["trace_id"] == "trace-fixed-1"


@pytest.mark.asyncio
async def test_failed_turn_keeps_real_stage_and_never_fakes_final_reply(
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """OBS-03：返回失败不做 final_reply；终态快照与事件停在真实失败阶段。"""
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def failing(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event("stage", {"stage": "supervisor", "status": "running", "attempt": 1})
        await on_event("stage", {"stage": "supervisor", "status": "failed", "attempt": 1,
                                 "error_category": "llm_error"})
        return ChatTurn(status="failed", error="模型拒绝了", error_category="llm_error",
                        latency_ms=12)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", failing)

    events = TurnEvents(cfg, turn_id="t-fail")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
    try:
        await asyncio.wait_for(task, timeout=5)
        await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    stage_events = [item["data"] for item in received if item["event"] == "stage"]
    assert all(item["stage"] != "final_reply" for item in stage_events)   # 不臆造收尾阶段
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
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """OBS-04 点 4：收尾写库失败 → 明确记录"未保存"，事件照常广播，不谎称已落库。"""
    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
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
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=uuid.uuid4(),
                      history=[], user_text="跑一下")
    try:
        with caplog.at_level(logging.WARNING, logger="xhs_agent.chat"):
            await asyncio.wait_for(task, timeout=5)
            await _wait_until(lambda: any(item["event"] == "error" for item in received))
    finally:
        pump.cancel()
        await events.aclose()

    assert any("轮次收尾写库失败" in item.getMessage() for item in caplog.records)
    assert received[-1]["event"] == "error"


@pytest.mark.asyncio
async def test_worker_stage_updates_persist_real_status_retries_and_identity(
        cfg: AppConfig, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3（OBS-05）：worker 的 failed / 重试 / skipped / run 身份都进快照，不再一律 running。

    失败尝试仍可追查（`nodes` 保留 clue/attempt=1/failed），重试成功不抹掉失败记录。
    """
    saved: list[tuple[uuid.UUID, dict[str, Any]]] = []

    async def fake_update(_session: Any, message_id: uuid.UUID,
                          **fields: Any) -> dict[str, Any]:
        saved.append((message_id, fields))
        return {}

    async def structured_run(*_args: Any, **kwargs: Any) -> Any:
        on_event = kwargs["on_event"]
        await on_event("stage", {"stage": "submit_analysis", "status": "running", "attempt": 1})
        await on_event("tool_progress", {"tool": "run_hotspot_analysis", "stage": "拆解",
                                         "stage_key": "clue", "status": "failed",
                                         "attempt": 1, "retries": 2,
                                         "error_category": "llm_error",
                                         "run_id": "run-9", "job_id": "job-9"})
        await on_event("tool_progress", {"tool": "run_hotspot_analysis", "stage": "拆解",
                                         "stage_key": "clue", "status": "running",
                                         "attempt": 2, "retries": 2, "error_category": "",
                                         "run_id": "run-9", "job_id": "job-9"})
        await on_event("tool_progress", {"tool": "run_hotspot_analysis", "stage": "报告",
                                         "stage_key": "report", "status": "succeeded",
                                         "attempt": 1, "retries": 0, "error_category": "",
                                         "run_id": "run-9", "job_id": "job-9",
                                         "latency_ms": 7})
        return ChatTurn(status="succeeded", reply="好了", run_id="run-9", cost_cny=0.05)

    engine = DummyEngine()
    _patch_turn_harness(monkeypatch, engine, fake_update, NullTracer())
    monkeypatch.setattr(chat_turns, "run_turn", structured_run)

    events = TurnEvents(cfg, turn_id="t-worker")
    await events.open()
    received: list[dict[str, Any]] = []
    pump = asyncio.create_task(_collect(events, received))
    message_id = uuid.uuid4()
    task = start_turn(cfg, events, session_id=uuid.uuid4(), message_id=message_id,
                      history=[], user_text="跑一下")
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
    assert snapshot["retries"] == 0                     # 当前阶段（报告）的投递重试数
    nodes = {(item["stage"], item["attempt"], item.get("run_id") or ""): item
             for item in snapshot["nodes"]}
    assert nodes[("clue", 1, "run-9")]["status"] == "failed"           # 失败尝试可追查
    assert nodes[("clue", 1, "run-9")]["error_category"] == "llm_error"
    assert nodes[("clue", 2, "run-9")]["status"] == "succeeded"        # 重试成功不抹掉失败
    assert nodes[("report", 1, "run-9")]["latency_ms"] == 7
    skipped_like = [item for item in snapshot["nodes"] if item["stage"] == "submit_analysis"]
    assert skipped_like and skipped_like[0]["status"] == "succeeded"   # 自有阶段照常收尾



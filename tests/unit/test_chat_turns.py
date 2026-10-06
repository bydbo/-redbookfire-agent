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
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from xhs_agent.api.routers import chat as chat_router
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import DependencyUnavailableError
from xhs_agent.services import chat_turns
from xhs_agent.services.chat_turns import TurnEvents, redis_url, sse_frame, start_turn
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult

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
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client: ScriptedProvider("跑好了", ["跑", "好了"]))

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
    monkeypatch.setattr(chat_turns, "create_engine_from_config", lambda _cfg: engine)
    monkeypatch.setattr(chat_turns, "create_session_factory", lambda _engine: dummy_factory)
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client: ScriptedProvider())
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

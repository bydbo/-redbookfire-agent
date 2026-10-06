"""S7.4 对话接口的集成用例（真 Postgres + 真 Redis 容器 + 假模型 / 假工具）。

覆盖「一次对话轮次」的完整闭环：

1. 建会话 → 发「图 + 一句话」→ 读 SSE（事件顺序与工具过程）→ 消息落库字段正确；
2. 附件真写进 `runs/_chat/<session_id>/`，并且能按 session + message + 下标取回；
3. **没有任何订阅者**时轮次照样跑完并落库（SSE 断开不停这一轮的硬证明）；
4. 过期 `running` 行在读会话时被标 `interrupted`；404 / 400 分支。

不联网：模型与工具执行器都注入替身；Redis 用容器里的真实例（事件总线的真实通路）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_dispatcher, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import ChatMessage
from xhs_agent.services import chat as chat_service
from xhs_agent.services import chat_turns
from xhs_agent.services.chat_tools import TOOL_RUN_ANALYSIS, ToolOutcome
from xhs_agent.services.chat_turns import TurnEvents, start_turn
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64        # 校验只看 magic bytes，内容不必是真图片
RUN_ID = str(uuid.uuid4())


class FakeDispatcher:
    """假投递器：只记账，不起 Celery（本用例不跑真分析）。"""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


class ScriptedProvider(llm.BaseProvider):
    """按剧本回话：第 N 次调用取第 N 个剧本，用完就道别。"""

    name = "scripted"

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__(model="scripted-1")
        self.steps = list(steps)
        self.calls: list[llm.LLMCall] = []

    async def complete(self, call: llm.LLMCall) -> LLMResult:      # pragma: no cover
        raise AssertionError("对话用例不该走 complete()")

    async def complete_with_tools(self, call: llm.LLMCall,
                                  on_delta: Any = None) -> LLMResult:
        self.calls.append(call)
        step = self.steps.pop(0) if self.steps else {"text": "（剧本用完了）"}
        if on_delta is not None:
            for piece in list(step.get("deltas") or []):
                await on_delta(piece)
        return LLMResult(text=str(step.get("text") or ""), provider=self.name,
                         model=self.model, tool_calls=list(step.get("tool_calls") or []),
                         prompt_tokens=10, completion_tokens=5, cost_cny=0.002)


class FakeToolRunner:
    """假工具执行器：记账 + 走一遍进度回调（真工具要投递 Celery，本用例不需要）。"""

    def __init__(self, outcome: ToolOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: dict[str, Any], *,
                       on_progress: Any = None) -> ToolOutcome:
        self.calls.append((name, args))
        if on_progress is not None:
            await on_progress("解析图片", 0, 0)
            await on_progress("分析中", 50, 100)
        return self.outcome


def tool_call(arguments: str = '{"hotspot": "夜跑", "topk": 5}') -> dict[str, Any]:
    return {"id": "call-1", "name": TOOL_RUN_ANALYSIS, "arguments": arguments}


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 `text/event-stream` 正文拆成 `[(事件名, data), ...]`（跳过 `:` 心跳）。"""
    events: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    data: list[str] = []
    for line in body.splitlines():
        if line.startswith(":"):
            continue
        if not line:
            if name is not None:
                events.append((name, json.loads("".join(data))))
                name, data = None, []
            continue
        if line.startswith("event: "):
            name = line[len("event: "):]
        elif line.startswith("data: "):
            data.append(line[len("data: "):])
    return events


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_dsn: str,
        redis_url: str) -> AppConfig:
    """临时配置：库 / Redis 指向容器，产物落 tmp；`[chat]` 全用默认值。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[frontend]\n"
        "serve = false\n",
        encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", postgres_dsn)
    monkeypatch.setenv("REDIS_URL", redis_url)
    return load_config(str(path))


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> ScriptedProvider:
    scripted = ScriptedProvider([{"text": "把热点文字发我，或者丢张图。"}])
    monkeypatch.setattr(chat_turns, "build_turn_provider",
                        lambda _cfg, _client: scripted)
    return scripted


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeToolRunner:
    runner = FakeToolRunner(ToolOutcome(
        status="succeeded",
        payload={"run_id": RUN_ID, "status": "succeeded", "candidates": 5,
                 "top1_title": "球场热身", "cost_cny": 0.05, "latency_ms": 1200},
        steps=[{"stage": "完成", "done": 100, "total": 100}]))
    monkeypatch.setattr(chat_turns, "build_tool_runner", lambda *_args, **_kwargs: runner)
    return runner


@pytest.fixture
def dispatcher() -> FakeDispatcher:
    return FakeDispatcher()


@pytest.fixture
def client(db_engine: AsyncEngine, cfg: AppConfig, dispatcher: FakeDispatcher
           ) -> Iterator[TestClient]:
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def new_session(client: TestClient) -> str:
    response = client.post("/api/chat/sessions")
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


class TestChatApi:
    def test_send_image_and_text_streams_tool_then_answer(
            self, client: TestClient, provider: ScriptedProvider,
            tools: FakeToolRunner, cfg: AppConfig) -> None:
        provider.steps = [
            {"text": "", "tool_calls": [tool_call()]},
            {"text": "跑了完整分析，命中 5 条。", "deltas": ["跑了", "完整分析，命中 5 条。"]},
        ]
        session_id = new_session(client)

        response = client.post(f"/api/chat/sessions/{session_id}/messages",
                               data={"text": "帮我蹭一下这个热点"},
                               files={"file": ("热点.png", PNG, "image/png")})
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)

        assert [name for name, _data in events] == [
            "turn_started", "tool_started", "tool_progress", "tool_progress",
            "tool_finished", "text_delta", "text_delta", "turn_finished"]
        assert tools.calls == [(TOOL_RUN_ANALYSIS, {"hotspot": "夜跑", "topk": 5})]
        assert events[1][1]["args"] == {"hotspot": "夜跑", "topk": 5}
        assert events[3][1] == {"tool": TOOL_RUN_ANALYSIS, "stage": "分析中",
                                "done": 50, "total": 100}
        assert [data["text"] for name, data in events if name == "text_delta"] == \
            ["跑了", "完整分析，命中 5 条。"]
        finished = events[-1][1]
        assert finished["status"] == "succeeded" and finished["run_id"] == RUN_ID

        # 消息落库：正文 / 工具过程 / run_id / 成本（两次 supervisor + 一次分析）
        detail = client.get(f"/api/chat/sessions/{session_id}").json()
        assert detail["title"] == "帮我蹭一下这个热点"
        assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
        user_message, assistant = detail["messages"]
        assert user_message["status"] == "succeeded"
        assert assistant["status"] == "succeeded"
        assert assistant["content"] == "跑了完整分析，命中 5 条。"
        assert assistant["run_id"] == RUN_ID
        assert assistant["cost_cny"] == pytest.approx(0.054)
        assert assistant["prompt_versions"] == {"chat_supervisor": 1}
        entry = assistant["tool_calls"][0]
        assert entry["tool"] == TOOL_RUN_ANALYSIS and entry["status"] == "succeeded"
        assert entry["run_id"] == RUN_ID and entry["args"] == {"hotspot": "夜跑", "topk": 5}

        # 附件：落盘 + 能从接口原样取回
        attachment = user_message["attachments"][0]
        assert attachment["kind"] == "image" and attachment["bytes"] == len(PNG)
        assert attachment["url"] == (f"/api/chat/sessions/{session_id}"
                                     f"/attachments/{user_message['message_id']}/0")
        stored = list((Path(cfg.runs_dir()) / "_chat" / session_id).glob("*.png"))
        assert len(stored) == 1 and stored[0].read_bytes() == PNG
        fetched = client.get(attachment["url"])
        assert fetched.status_code == 200
        assert fetched.content == PNG

        # 会话列表：标题、消息数、末条预览
        listing = client.get("/api/chat/sessions").json()
        assert listing["total"] == 1
        assert listing["items"][0]["session_id"] == session_id
        assert listing["items"][0]["message_count"] == 2
        assert listing["items"][0]["last_message_preview"] == "跑了完整分析，命中 5 条。"

    def test_tool_failure_is_reported_back_without_failing_the_turn(
            self, client: TestClient, provider: ScriptedProvider,
            tools: FakeToolRunner) -> None:
        tools.outcome = ToolOutcome(status="failed", error="素材目录不存在")
        provider.steps = [
            {"text": "", "tool_calls": [tool_call(arguments='{"hotspot": "夜跑"}')]},
            {"text": "这次没跑成：素材目录不存在。"},
        ]
        session_id = new_session(client)

        response = client.post(f"/api/chat/sessions/{session_id}/messages",
                               data={"text": "帮我蹭一下这个热点"})
        events = parse_sse(response.text)

        assert [name for name, _data in events] == [
            "turn_started", "tool_started", "tool_progress", "tool_progress",
            "tool_finished", "turn_finished"]
        detail = client.get(f"/api/chat/sessions/{session_id}").json()
        assistant = detail["messages"][-1]
        assert assistant["status"] == "succeeded"           # 工具失败不炸整轮
        assert assistant["content"] == "这次没跑成：素材目录不存在。"
        assert assistant["tool_calls"][0]["status"] == "failed"
        assert assistant["tool_calls"][0]["error"] == "素材目录不存在"
        assert json.loads(provider.calls[1].messages[-1]["content"])["status"] == "failed"

    @pytest.mark.asyncio
    async def test_turn_completes_without_any_subscriber(
            self, client: TestClient, cfg: AppConfig, db_engine: AsyncEngine,
            provider: ScriptedProvider, tools: FakeToolRunner) -> None:
        """SSE 断开（没有人订阅事件）时，detached 的轮次照常跑完并落库。"""
        provider.steps = [{"text": "先给你一段话。"}]
        session_id = uuid.UUID(new_session(client))

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            await chat_service.append_message(session, session_id, role="user",
                                              content="帮我蹭一下这个热点")
            assistant = await chat_service.append_message(
                session, session_id, role="assistant", status="running")
            assert assistant is not None
        events = TurnEvents(cfg, turn_id=str(uuid.uuid4()))
        task = start_turn(cfg, events, session_id=session_id,
                          message_id=uuid.UUID(str(assistant["message_id"])),
                          history=[], user_text="帮我蹭一下这个热点")
        await asyncio.wait_for(task, timeout=60)
        async with factory() as session:
            detail = await chat_service.load_session(session, session_id)
        assert detail is not None
        assistant = detail["messages"][-1]
        assert assistant["status"] == "succeeded"
        assert assistant["content"] == "先给你一段话。"

    @pytest.mark.asyncio
    async def test_stale_running_message_is_marked_interrupted(
            self, client: TestClient, db_session: AsyncSession) -> None:
        session_id = new_session(client)
        db_session.add(ChatMessage(session_id=uuid.UUID(session_id), role="assistant",
                                   content="", status="running",
                                   created_at=datetime.now(UTC) - timedelta(seconds=10_000)))
        db_session.add(ChatMessage(session_id=uuid.UUID(session_id), role="assistant",
                                   content="刚刚才开始跑", status="running"))
        await db_session.commit()

        detail = client.get(f"/api/chat/sessions/{session_id}").json()
        assistant = [item for item in detail["messages"] if item["role"] == "assistant"]
        assert [item["status"] for item in assistant] == ["interrupted", "running"]
        assert "没跑完" in assistant[0]["error"]

    def test_error_branches(self, client: TestClient) -> None:
        session_id = new_session(client)
        missing = str(uuid.uuid4())

        assert client.get(f"/api/chat/sessions/{missing}").status_code == 404
        assert client.get("/api/chat/sessions/not-a-uuid").status_code == 404
        assert client.post(f"/api/chat/sessions/{missing}/messages",
                           data={"text": "嗨"}).status_code == 404
        # 既没文字也没图片
        assert client.post(f"/api/chat/sessions/{session_id}/messages").status_code == 400
        # 不是 jpg / png / webp
        bad = client.post(f"/api/chat/sessions/{session_id}/messages",
                          data={"text": "看看这张"},
                          files={"file": ("x.gif", b"GIF89a" + b"\x00" * 8, "image/gif")})
        assert bad.status_code == 400
        # 文字超 500 字
        too_long = client.post(f"/api/chat/sessions/{session_id}/messages",
                               data={"text": "热" * 501})
        assert too_long.status_code == 400
        # 附件取回：下标越界 / 消息不属于该会话 / 文件不存在
        assert client.get(f"/api/chat/sessions/{session_id}/attachments/"
                          f"{uuid.uuid4()}/0").status_code == 404

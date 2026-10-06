"""对话存储的集成用例（S7.2）：真 Postgres 容器 + 临时 runs 目录。

在接口与编排接入之前，先把"会话 / 消息 / 附件"这一层的事实钉死：
标题来自首条用户消息、列表按活跃时间倒序、running 行可更新为终态、过期 running 会被标中断、
附件落到 `runs/_chat/<session_id>/` 且能按会话取回（越界一律 404）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import NotFoundError
from xhs_agent.db import Base
from xhs_agent.services.chat import (
    append_message,
    create_session,
    list_sessions,
    load_message,
    load_session,
    mark_stale_turns,
    resolve_attachment,
    save_attachment,
    update_message,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def write_config(tmp_path: Path) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return write_config(tmp_path)


class TestSessions:
    async def test_new_session_has_placeholder_title(self, db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        assert created["title"] == "新对话"

        loaded = await load_session(db_session, uuid.UUID(created["session_id"]))
        assert loaded is not None
        assert loaded["messages"] == []
        assert loaded["created_at"] and loaded["updated_at"]

    async def test_first_user_message_sets_title(self, db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])

        await append_message(db_session, session_id, role="user",
                             content="  帮我蹭一下这个热点：明星打羽毛球被拍  ")
        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        assert loaded["title"] == "帮我蹭一下这个热点：明星打羽毛球被拍"

        await append_message(db_session, session_id, role="user", content="换一个话题")
        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        assert loaded["title"] == "帮我蹭一下这个热点：明星打羽毛球被拍"   # 标题只认首条

    async def test_title_falls_back_for_blank_message(self, db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        await append_message(db_session, session_id, role="user", content="   ")

        loaded = await load_session(db_session, session_id)
        assert loaded is not None and loaded["title"] == "新对话"

    async def test_list_orders_by_updated_at_with_counts_and_preview(
            self, db_session: AsyncSession) -> None:
        first = await create_session(db_session)
        second = await create_session(db_session)
        third = await create_session(db_session)
        first_id = uuid.UUID(first["session_id"])
        second_id = uuid.UUID(second["session_id"])
        third_id = uuid.UUID(third["session_id"])

        await append_message(db_session, first_id, role="user", content="最早")
        await append_message(db_session, third_id, role="user",
                             content="最" + "长" * 80)          # 超 60 字要截断
        # 让 first 变成最近活跃的会话
        await db_session.execute(text(
            "UPDATE chat_sessions SET updated_at = now() WHERE id = :i"), {"i": first_id})
        await db_session.commit()

        payload = await list_sessions(db_session)

        assert payload["total"] == 3
        assert [item["session_id"] for item in payload["items"]][:2] == [
            str(first_id), str(third_id)]
        by_id = {item["session_id"]: item for item in payload["items"]}
        assert by_id[str(third_id)]["message_count"] == 1
        assert by_id[str(third_id)]["last_message_preview"].endswith("…")
        assert len(by_id[str(third_id)]["last_message_preview"]) == 61
        assert by_id[str(second_id)]["message_count"] == 0
        assert by_id[str(second_id)]["last_message_preview"] == ""

    async def test_append_to_unknown_session_returns_none(self,
                                                         db_session: AsyncSession) -> None:
        assert await append_message(db_session, uuid.uuid4(), role="user",
                                    content="x") is None

    async def test_running_message_can_be_finalised(self, db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        await append_message(db_session, session_id, role="user", content="一句话")

        message = await append_message(db_session, session_id, role="assistant",
                                       status="running")
        assert message is not None and message["status"] == "running"

        run_id = str(uuid.uuid4())
        updated = await update_message(
            db_session, uuid.UUID(message["message_id"]), content="跑完了，这是结果",
            status="succeeded", run_id=run_id, cost_cny=0.053, latency_ms=26000,
            tool_calls=[{"tool": "run_hotspot_analysis", "status": "succeeded",
                         "run_id": run_id, "latency_ms": 26000}],
            prompt_versions={"chat_supervisor": 1})
        assert updated is not None
        assert updated["status"] == "succeeded"
        assert updated["content"] == "跑完了，这是结果"
        assert updated["run_id"] == run_id
        assert updated["cost_cny"] == pytest.approx(0.053)
        assert updated["latency_ms"] == 26000
        assert updated["tool_calls"][0]["tool"] == "run_hotspot_analysis"
        assert updated["prompt_versions"] == {"chat_supervisor": 1}

        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        assert [item["role"] for item in loaded["messages"]] == ["user", "assistant"]
        assert loaded["messages"][1]["content"] == "跑完了，这是结果"

    async def test_update_unknown_message_returns_none(self,
                                                       db_session: AsyncSession) -> None:
        assert await update_message(db_session, uuid.uuid4(), content="x") is None

    async def test_stale_running_turns_are_marked_interrupted(
            self, db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        stale = await append_message(db_session, session_id, role="assistant",
                                     status="running")
        fresh = await append_message(db_session, session_id, role="assistant",
                                     status="running")
        assert stale is not None and fresh is not None
        # 把其中一条的 created_at 拨到 10 分钟前（模拟进程重启遗留）
        await db_session.execute(text(
            "UPDATE chat_messages SET created_at = :t WHERE id = :i"),
            {"t": datetime.now(UTC) - timedelta(minutes=10),
             "i": uuid.UUID(stale["message_id"])})
        await db_session.commit()

        changed = await mark_stale_turns(db_session, stale_seconds=300,
                                         session_id=session_id)

        assert changed == 1
        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        by_id = {item["message_id"]: item for item in loaded["messages"]}
        assert by_id[stale["message_id"]]["status"] == "interrupted"
        assert by_id[stale["message_id"]]["error"]
        assert by_id[fresh["message_id"]]["status"] == "running"

    async def test_load_session_limit_keeps_the_latest(self,
                                                       db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        for index in range(5):
            await append_message(db_session, session_id, role="user", content=f"第{index}条")

        loaded = await load_session(db_session, session_id, limit=2)

        assert loaded is not None
        assert [item["content"] for item in loaded["messages"]] == ["第3条", "第4条"]


class TestAttachments:
    async def test_saved_attachment_is_served_with_url(self, db_session: AsyncSession,
                                                       cfg: AppConfig, tmp_path: Path) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        saved = save_attachment(cfg, session_id, b"\x89PNG\r\n\x1a\nfake", name="热点.png",
                                mime="image/png")

        message = await append_message(db_session, session_id, role="user", content="看图",
                                       attachments=[saved])

        assert message is not None
        attachment = message["attachments"][0]
        assert attachment["kind"] == "image" and attachment["mime"] == "image/png"
        assert attachment["bytes"] == len(b"\x89PNG\r\n\x1a\nfake")
        assert attachment["url"] == (f"/api/chat/sessions/{session_id}"
                                     f"/attachments/{message['message_id']}/0")
        assert Path(cfg.runs_dir(), "_chat", str(session_id)).is_dir()

        resolved = resolve_attachment(cfg, session_id, attachment["path"])
        assert Path(resolved).read_bytes() == b"\x89PNG\r\n\x1a\nfake"

    async def test_attachment_of_another_session_is_not_found(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        first = uuid.UUID((await create_session(db_session))["session_id"])
        second = uuid.UUID((await create_session(db_session))["session_id"])
        saved = save_attachment(cfg, first, b"\x89PNG", name="a.png", mime="image/png")

        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, second, saved["path"])

    async def test_traversal_path_is_not_found(self, cfg: AppConfig) -> None:
        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, uuid.uuid4(), "../data/materials/秘密.mp4")

    async def test_load_message_returns_view(self, db_session: AsyncSession,
                                             cfg: AppConfig) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        saved = save_attachment(cfg, session_id, b"\x89PNG", name="a.png", mime="image/png")
        message = await append_message(db_session, session_id, role="user",
                                       attachments=[saved])
        assert message is not None

        loaded = await load_message(db_session, uuid.UUID(message["message_id"]))

        assert loaded is not None and loaded["attachments"][0]["name"] == "a.png"
        assert await load_message(db_session, uuid.uuid4()) is None

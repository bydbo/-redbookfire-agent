"""对话存储的集成用例（S7.2）：真 Postgres 容器 + 临时 runs 目录。

在接口与编排接入之前，先把"会话 / 消息 / 附件"这一层的事实钉死：
标题来自首条用户消息、列表按活跃时间倒序、running 行可更新为终态、过期 running 会被标中断、
附件落到 `runs/_chat/<session_id>/` 且能按会话取回（越界一律 404）。
"""

from __future__ import annotations

import asyncio
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
    latest_attachments,
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
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n',
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
    async def test_new_session_has_placeholder_title(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        assert created["title"] == "新对话"

        loaded = await load_session(db_session, uuid.UUID(created["session_id"]))
        assert loaded is not None
        assert loaded["messages"] == []
        assert loaded["created_at"] and loaded["updated_at"]

    async def test_first_user_message_sets_title(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])

        await append_message(
            db_session,
            session_id,
            role="user",
            content="  帮我蹭一下这个热点：明星打羽毛球被拍  ",
        )
        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        assert loaded["title"] == "帮我蹭一下这个热点：明星打羽毛球被拍"

        await append_message(db_session, session_id, role="user", content="换一个话题")
        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        assert loaded["title"] == "帮我蹭一下这个热点：明星打羽毛球被拍"  # 标题只认首条

    async def test_title_falls_back_for_blank_message(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        await append_message(db_session, session_id, role="user", content="   ")

        loaded = await load_session(db_session, session_id)
        assert loaded is not None and loaded["title"] == "新对话"

    async def test_list_orders_by_updated_at_with_counts_and_preview(
        self, db_session: AsyncSession
    ) -> None:
        first = await create_session(db_session)
        second = await create_session(db_session)
        third = await create_session(db_session)
        first_id = uuid.UUID(first["session_id"])
        second_id = uuid.UUID(second["session_id"])
        third_id = uuid.UUID(third["session_id"])

        await append_message(db_session, first_id, role="user", content="最早")
        await append_message(
            db_session, third_id, role="user", content="最" + "长" * 80
        )  # 超 60 字要截断
        # 让 first 变成最近活跃的会话
        await db_session.execute(
            text("UPDATE chat_sessions SET updated_at = now() WHERE id = :i"),
            {"i": first_id},
        )
        await db_session.commit()

        payload = await list_sessions(db_session)

        assert payload["total"] == 3
        assert [item["session_id"] for item in payload["items"]][:2] == [
            str(first_id),
            str(third_id),
        ]
        by_id = {item["session_id"]: item for item in payload["items"]}
        assert by_id[str(third_id)]["message_count"] == 1
        assert by_id[str(third_id)]["last_message_preview"].endswith("…")
        assert len(by_id[str(third_id)]["last_message_preview"]) == 61
        assert by_id[str(second_id)]["message_count"] == 0
        assert by_id[str(second_id)]["last_message_preview"] == ""

    async def test_append_to_unknown_session_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        assert (
            await append_message(db_session, uuid.uuid4(), role="user", content="x")
            is None
        )

    async def test_running_message_can_be_finalised(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        await append_message(db_session, session_id, role="user", content="一句话")

        message = await append_message(
            db_session, session_id, role="assistant", status="running"
        )
        assert message is not None and message["status"] == "running"

        run_id = str(uuid.uuid4())
        updated = await update_message(
            db_session,
            uuid.UUID(message["message_id"]),
            content="跑完了，这是结果",
            status="succeeded",
            run_id=run_id,
            cost_cny=0.053,
            latency_ms=26000,
            tool_calls=[
                {
                    "tool": "run_hotspot_analysis",
                    "status": "succeeded",
                    "run_id": run_id,
                    "latency_ms": 26000,
                }
            ],
            prompt_versions={"chat_supervisor": 1},
        )
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

    async def test_update_unknown_message_returns_none(
        self, db_session: AsyncSession
    ) -> None:
        assert await update_message(db_session, uuid.uuid4(), content="x") is None

    async def test_stale_running_turns_are_marked_interrupted(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        stale = await append_message(
            db_session, session_id, role="assistant", status="running"
        )
        fresh = await append_message(
            db_session, session_id, role="assistant", status="running"
        )
        assert stale is not None and fresh is not None
        # 把其中一条的 created_at 拨到 10 分钟前（模拟进程重启遗留）
        await db_session.execute(
            text("UPDATE chat_messages SET created_at = :t WHERE id = :i"),
            {
                "t": datetime.now(UTC) - timedelta(minutes=10),
                "i": uuid.UUID(stale["message_id"]),
            },
        )
        await db_session.commit()

        changed = await mark_stale_turns(
            db_session, stale_seconds=300, session_id=session_id
        )

        assert changed == 1
        loaded = await load_session(db_session, session_id)
        assert loaded is not None
        by_id = {item["message_id"]: item for item in loaded["messages"]}
        assert by_id[stale["message_id"]]["status"] == "interrupted"
        assert by_id[stale["message_id"]]["error"]
        assert by_id[fresh["message_id"]]["status"] == "running"

    async def test_load_session_limit_keeps_the_latest(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        for index in range(5):
            await append_message(
                db_session, session_id, role="user", content=f"第{index}条"
            )

        loaded = await load_session(db_session, session_id, limit=2)

        assert loaded is not None
        assert [item["content"] for item in loaded["messages"]] == ["第3条", "第4条"]


class TestAttachments:
    async def test_saved_attachment_is_served_with_url(
        self, db_session: AsyncSession, cfg: AppConfig, tmp_path: Path
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        saved = save_attachment(
            cfg, session_id, b"\x89PNG\r\n\x1a\nfake", name="热点.png", mime="image/png"
        )

        message = await append_message(
            db_session, session_id, role="user", content="看图", attachments=[saved]
        )

        assert message is not None
        attachment = message["attachments"][0]
        assert attachment["kind"] == "image" and attachment["mime"] == "image/png"
        assert attachment["bytes"] == len(b"\x89PNG\r\n\x1a\nfake")
        assert attachment["url"] == (
            f"/api/chat/sessions/{session_id}/attachments/{message['message_id']}/0"
        )
        assert Path(cfg.runs_dir(), "_chat", str(session_id)).is_dir()

        resolved = resolve_attachment(cfg, session_id, attachment["path"])
        assert Path(resolved).read_bytes() == b"\x89PNG\r\n\x1a\nfake"

    async def test_attachment_of_another_session_is_not_found(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        first = uuid.UUID((await create_session(db_session))["session_id"])
        second = uuid.UUID((await create_session(db_session))["session_id"])
        saved = save_attachment(cfg, first, b"\x89PNG", name="a.png", mime="image/png")

        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, second, saved["path"])

    async def test_traversal_path_is_not_found(self, cfg: AppConfig) -> None:
        with pytest.raises(NotFoundError):
            resolve_attachment(cfg, uuid.uuid4(), "../data/materials/秘密.mp4")

    async def test_load_message_returns_view(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        saved = save_attachment(
            cfg, session_id, b"\x89PNG", name="a.png", mime="image/png"
        )
        message = await append_message(
            db_session, session_id, role="user", attachments=[saved]
        )
        assert message is not None

        loaded = await load_message(db_session, uuid.UUID(message["message_id"]))

        assert loaded is not None and loaded["attachments"][0]["name"] == "a.png"
        assert await load_message(db_session, uuid.uuid4()) is None


class TestUsage:
    """轮级用量落库（S7.10）：`prompt_tokens` / `completion_tokens` 与 `cost_cny` 同进同出。"""

    async def test_tokens_round_trip_through_append_and_update(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])

        row = await append_message(
            db_session,
            session_id,
            role="assistant",
            content="跑好了",
            prompt_tokens=1234,
            completion_tokens=567,
            cost_cny=0.0123,
        )
        assert row is not None
        assert (row["prompt_tokens"], row["completion_tokens"]) == (1234, 567)

        updated = await update_message(
            db_session,
            uuid.UUID(row["message_id"]),
            prompt_tokens=2000,
            completion_tokens=800,
        )
        assert updated is not None
        assert (updated["prompt_tokens"], updated["completion_tokens"]) == (2000, 800)
        assert updated["cost_cny"] == pytest.approx(0.0123)  # 只改显式给了的字段


class TestLatestAttachments:
    """跨轮图片回落（S7.10 起、S7.16 改成整组）：最近**一条带图消息的整组**，且绝不跨会话。"""

    async def test_returns_the_whole_group_of_the_newest_message(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        older = save_attachment(
            cfg, session_id, b"\x89PNG-old", name="旧图.png", mime="image/png"
        )
        newer = [
            save_attachment(
                cfg,
                session_id,
                b"\x89PNG-new",
                name=f"新图{index}.png",
                mime="image/png",
            )
            for index in (1, 2)
        ]

        await append_message(
            db_session, session_id, role="user", content="第一张", attachments=[older]
        )
        await asyncio.sleep(0.01)  # 保证两条消息的 created_at 不同（排序才无歧义）
        await append_message(
            db_session, session_id, role="user", content="第二组", attachments=newer
        )

        found = await latest_attachments(db_session, session_id)
        # 整组、按发送顺序（顺序即图号，多图解析要靠它指图）
        assert [item["name"] for item in found] == ["新图1.png", "新图2.png"]

    async def test_empty_when_the_session_has_no_image(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        await append_message(db_session, session_id, role="user", content="只有文字")
        assert await latest_attachments(db_session, session_id) == []

    async def test_never_crosses_sessions(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        first = uuid.UUID((await create_session(db_session))["session_id"])
        second = uuid.UUID((await create_session(db_session))["session_id"])
        saved = save_attachment(
            cfg, second, b"\x89PNG", name="乙图.png", mime="image/png"
        )
        await append_message(
            db_session, second, role="user", content="乙会话的图", attachments=[saved]
        )

        assert await latest_attachments(db_session, first) == []
        found = await latest_attachments(db_session, second)
        assert [item["name"] for item in found] == ["乙图.png"]


class TestTurnProgress:
    """S7.21：`turn_id` / `trace_id` / `progress` 的读写与过期语义。"""

    async def test_append_and_update_round_trip(self, db_session: AsyncSession) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        turn_id = str(uuid.uuid4())
        message = await append_message(
            db_session,
            session_id,
            role="assistant",
            status="running",
            turn_id=turn_id,
            progress={
                "stage": "supervisor",
                "status": "running",
                "started_at": "2026-10-15T08:00:00+00:00",
            },
        )
        assert message is not None
        assert message["turn_id"] == turn_id
        assert message["trace_id"] is None  # 追踪关闭 / 未收尾：如实为空
        assert message["progress"]["stage"] == "supervisor"

        updated = await update_message(
            db_session,
            uuid.UUID(message["message_id"]),
            status="succeeded",
            trace_id="trace-abc",
            progress={"stage": "final_reply", "status": "succeeded"},
        )
        assert updated is not None
        assert updated["trace_id"] == "trace-abc"
        assert updated["progress"]["stage"] == "final_reply"

        # 旧行口径：不带新字段的消息，读出空快照（对外折成 null）与空 trace，不报错
        plain = await append_message(
            db_session, session_id, role="user", content="旧口径"
        )
        assert plain is not None
        assert plain["progress"] is None
        assert plain["trace_id"] is None

        # 半成品快照（崩溃兜底路径可能写入）在读取侧也要折成 null，不能让响应模型 500
        malformed = await append_message(
            db_session, session_id, role="assistant", progress={"status": ""}
        )
        assert malformed is not None
        assert malformed["progress"] is None

    async def test_mark_stale_marks_progress_interrupted(
        self, db_session: AsyncSession
    ) -> None:
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        message = await append_message(
            db_session,
            session_id,
            role="assistant",
            status="running",
            progress={"stage": "draft", "status": "running", "attempt": 1},
        )
        assert message is not None
        # 把 created_at 拨到很久以前，触发 `turn_stale_seconds` 的过期判定
        await db_session.execute(
            text(
                "UPDATE chat_messages SET created_at = now() - interval '2 hours' WHERE id = :i"
            ),
            {"i": uuid.UUID(message["message_id"])},
        )
        await db_session.commit()

        changed = await mark_stale_turns(db_session, stale_seconds=300)
        assert changed == 1

        loaded = await load_message(db_session, uuid.UUID(message["message_id"]))
        assert loaded is not None
        assert loaded["status"] == "interrupted"
        assert loaded["progress"]["status"] == "interrupted"  # 快照与消息同一生命周期
        assert loaded["progress"]["stage"] == "draft"


class TestStalenessByActivity:
    """R3（OBS-11）：陈旧判定按"最近活跃时间"（快照 updated_at），不再单看创建时间。"""

    @pytest.mark.asyncio
    async def test_active_heartbeat_prevents_false_interruption(
        self, db_session: AsyncSession
    ) -> None:
        """创建很早但心跳在跳（快照 updated_at 新）的活跃长轮次：不误判。"""
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        message = await append_message(
            db_session,
            session_id,
            role="assistant",
            status="running",
            progress={
                "stage": "draft",
                "label": "撰稿",
                "status": "running",
                "attempt": 1,
                "started_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        assert message is not None
        await db_session.execute(
            text(
                "UPDATE chat_messages SET created_at = now() - interval '2 hours' WHERE id = :i"
            ),
            {"i": uuid.UUID(message["message_id"])},
        )
        await db_session.commit()

        changed = await mark_stale_turns(db_session, stale_seconds=300)

        assert changed == 0  # 心跳活跃：不收敛
        loaded = await load_message(db_session, uuid.UUID(message["message_id"]))
        assert loaded is not None and loaded["status"] == "running"

    @pytest.mark.asyncio
    async def test_dead_heartbeat_is_marked_interrupted(
        self, db_session: AsyncSession
    ) -> None:
        """心跳停止（快照 updated_at 陈旧）：按活跃时间收敛为 interrupted。"""
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        message = await append_message(
            db_session,
            session_id,
            role="assistant",
            status="running",
            progress={
                "stage": "draft",
                "label": "撰稿",
                "status": "running",
                "attempt": 1,
                "updated_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
            },
        )
        assert message is not None
        await db_session.execute(
            text(
                "UPDATE chat_messages SET created_at = now() - interval '2 hours' WHERE id = :i"
            ),
            {"i": uuid.UUID(message["message_id"])},
        )
        await db_session.commit()

        changed = await mark_stale_turns(db_session, stale_seconds=300)

        assert changed == 1
        loaded = await load_message(db_session, uuid.UUID(message["message_id"]))
        assert loaded is not None and loaded["status"] == "interrupted"
        assert loaded["progress"]["status"] == "interrupted"


class TestFailedTurnPersistence:
    """R5（E7OBS2-01）：失败轮次的已知账目 / 工具过程 / 运行引用要**真的落库**。

    用真 Postgres 跑「工作流结果 → `_finish` 收尾 → 数据库 → 读回消息」全链，
    不只截获 mock 调用。运行引用用合法 UUID（不是 `r-1` 这类占位）。
    """

    async def test_failed_finish_persists_known_ledger_and_references(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        from xhs_agent.core import progress as progress_tool
        from xhs_agent.services import chat_turns
        from xhs_agent.services.chat_turns import TurnEvents
        from xhs_agent.workflows.chat import ChatTurn

        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        assistant = await append_message(
            db_session, session_id, role="assistant", status="running"
        )
        assert assistant is not None
        message_id = uuid.UUID(assistant["message_id"])
        run_id = uuid.uuid4()

        turn = ChatTurn(
            status="failed",
            error="第二次模型调用失败",
            error_category="llm_error",
            latency_ms=42,
            cost_cny=0.051,
            prompt_tokens=710,
            completion_tokens=305,
            run_id=str(run_id),
            prompt_versions={"chat_supervisor": 1, "run_hotspot_analysis": 1},
            usage_complete=False,
            usage_unknown_calls=1,
            tool_calls=[
                {
                    "tool": "run_hotspot_analysis",
                    "status": "succeeded",
                    "args": {},
                    "call_id": "c1",
                    "attempt": 1,
                    "run_id": str(run_id),
                    "cost_cny": 0.05,
                    "prompt_tokens": 700,
                    "completion_tokens": 300,
                    "latency_ms": 1200,
                    "error": "",
                    "steps": [],
                    "usage_status": "actual",
                }
            ],
        )
        snapshot = progress_tool.apply(
            None, stage="supervisor", status="failed", error_category="llm_error"
        )
        events = TurnEvents(cfg, turn_id=str(uuid.uuid4()))

        status = await chat_turns._finish(
            db_session,
            events,
            turn,
            message_id=message_id,
            session_id=session_id,
            snapshot=snapshot,
            trace_id="trace-e7obs2",
        )

        assert status == "failed"
        loaded = await load_message(db_session, message_id)
        assert loaded is not None
        # 刷新读回：工具过程、运行引用、账目、版本与 trace 都在
        assert loaded["status"] == "failed"
        assert loaded["tool_calls"][0]["tool"] == "run_hotspot_analysis"
        assert loaded["tool_calls"][0]["run_id"] == str(run_id)
        assert loaded["run_id"] == str(run_id)
        assert loaded["cost_cny"] == pytest.approx(0.051)
        assert loaded["prompt_tokens"] == 710
        assert loaded["completion_tokens"] == 305
        assert loaded["prompt_versions"] == {
            "chat_supervisor": 1,
            "run_hotspot_analysis": 1,
        }
        assert loaded["trace_id"] == "trace-e7obs2"
        assert loaded["progress"]["stage"] == "supervisor"
        assert loaded["progress"]["status"] == "failed"


class TestHeartbeatAndReconcilerInterlocks:
    """R5（E7OBS2-06）：续约只前滚活动时间；对账 CAS 不覆盖刚成功 / 刚续约的行。"""

    async def _running_with_snapshot(
        self,
        db_session: AsyncSession,
        *,
        stage: str = "draft",
        updated_at: datetime | None = None,
    ):
        created = await create_session(db_session)
        session_id = uuid.UUID(created["session_id"])
        message = await append_message(
            db_session,
            session_id,
            role="assistant",
            status="running",
            progress={
                "stage": stage,
                "label": "撰稿",
                "status": "running",
                "attempt": 1,
                "started_at": datetime.now(UTC).isoformat(),
                "updated_at": (updated_at or datetime.now(UTC)).isoformat(),
            },
        )
        assert message is not None
        message_id = uuid.UUID(message["message_id"])
        turn_id = uuid.uuid4()
        await db_session.execute(
            text("UPDATE chat_messages SET turn_id = :t WHERE id = :i"),
            {"t": turn_id, "i": message_id},
        )
        await db_session.commit()
        return message_id, turn_id

    @pytest.mark.asyncio
    async def test_renewal_never_overwrites_a_newer_stage(
        self, db_session: AsyncSession
    ) -> None:
        """心跳读旧 → 主流程写新阶段 → 心跳提交：新阶段不丢（原子续约只改 updated_at）。"""
        from xhs_agent.services.chat_turns import renew_turn_activity

        message_id, turn_id = await self._running_with_snapshot(db_session)
        # 主流程已经进入新阶段（此时"心跳手里"还是旧副本 stage=draft 的口径）
        await update_message(
            db_session,
            message_id,
            progress={
                "stage": "report",
                "label": "报告",
                "status": "running",
                "attempt": 1,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        affected = await renew_turn_activity(
            db_session, message_id=message_id, turn_id=str(turn_id)
        )
        await db_session.commit()

        assert affected == 1
        loaded = await load_message(db_session, message_id)
        assert loaded is not None
        assert loaded["progress"]["stage"] == "report"  # 旧副本覆盖不了新阶段
        assert loaded["progress"]["status"] == "running"

    @pytest.mark.asyncio
    async def test_renewal_after_terminal_is_a_noop(
        self, db_session: AsyncSession
    ) -> None:
        """终态之后的迟到心跳：0 行，不把终态改回 running。"""
        from xhs_agent.services.chat_turns import renew_turn_activity

        message_id, turn_id = await self._running_with_snapshot(db_session)
        await update_message(db_session, message_id, status="succeeded", content="好了")

        affected = await renew_turn_activity(
            db_session, message_id=message_id, turn_id=str(turn_id)
        )
        await db_session.commit()

        assert affected == 0
        loaded = await load_message(db_session, message_id)
        assert loaded is not None and loaded["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_reconciler_does_not_override_a_fresh_success(
        self, db_session: AsyncSession
    ) -> None:
        """对账选中（旧判定）→ 主流程成功 → 对账写回：CAS 重检，0 行、不覆盖终态。"""
        from xhs_agent.services.chat import _interrupt_stale_row

        message_id, _turn = await self._running_with_snapshot(
            db_session, updated_at=datetime.now(UTC) - timedelta(minutes=10)
        )
        cutoff = datetime.now(UTC) - timedelta(seconds=300)
        # 交错点：对账已把该行判定成候选；主流程随后成功
        await update_message(db_session, message_id, status="succeeded", content="好了")

        changed = await _interrupt_stale_row(
            db_session, message_id=message_id, cutoff=cutoff, detail="x"
        )
        await db_session.commit()

        assert changed == 0
        loaded = await load_message(db_session, message_id)
        assert loaded is not None and loaded["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_reconciler_does_not_override_a_fresh_renewal(
        self, db_session: AsyncSession
    ) -> None:
        """对账选中 → 心跳刚续约 → 对账写回：CAS 重检活跃时间，0 行、仍 running。"""
        from xhs_agent.services.chat import _interrupt_stale_row
        from xhs_agent.services.chat_turns import renew_turn_activity

        message_id, turn_id = await self._running_with_snapshot(
            db_session, updated_at=datetime.now(UTC) - timedelta(minutes=10)
        )
        cutoff = datetime.now(UTC) - timedelta(seconds=300)
        await renew_turn_activity(
            db_session, message_id=message_id, turn_id=str(turn_id)
        )
        await db_session.commit()

        changed = await _interrupt_stale_row(
            db_session, message_id=message_id, cutoff=cutoff, detail="x"
        )
        await db_session.commit()

        assert changed == 0
        loaded = await load_message(db_session, message_id)
        assert loaded is not None and loaded["status"] == "running"

    @pytest.mark.asyncio
    async def test_truly_dead_turn_still_converges(
        self, db_session: AsyncSession
    ) -> None:
        """真·失联（不再续约）：CAS 照常收敛，快照与类别都落。"""
        message_id, _turn = await self._running_with_snapshot(
            db_session, updated_at=datetime.now(UTC) - timedelta(minutes=10)
        )
        await db_session.execute(
            text(
                "UPDATE chat_messages SET created_at = now() - interval '2 hours' WHERE id = :i"
            ),
            {"i": message_id},
        )
        await db_session.commit()

        changed = await mark_stale_turns(db_session, stale_seconds=300)

        assert changed == 1
        loaded = await load_message(db_session, message_id)
        assert loaded is not None and loaded["status"] == "interrupted"
        assert loaded["progress"]["status"] == "interrupted"
        assert loaded["progress"]["error_category"] == "timeout"

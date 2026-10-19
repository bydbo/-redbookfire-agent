"""上下文状态、产物版本、图片缓存与观测的真实存储用例（S7.24–S7.28）。

真 Postgres 容器（`pytest -m integration` 才跑），钉住四类只有真库才能证明的事实：

1. 状态 CAS 与审计同事务：过期版本不推进、不留半个状态，`(session_id, state_version)` 唯一；
2. 版本链：初稿登记 → 认领 → 成功提交；同请求重放不推进、失败记录不占成功号、
   `expected_state_version` 变化时冲突且不覆盖 head；
3. 图片缓存：写入 → 命中 → 到期 miss → 关闭开关走 bypass → 同键重复写入只保留一条；
4. 观测：同一 `event_id` 重复投递只写一行。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import ConflictError
from xhs_agent.core.observations import record as record_observation
from xhs_agent.db import Base
from xhs_agent.db.models import (
    ChatContextEvent,
    ChatImageParseCache,
    ContextObservation,
)
from xhs_agent.schemas import ContextDelta
from xhs_agent.services import artifact_versions, context_state, image_cache
from xhs_agent.services.chat import create_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
RUN_ID = str(uuid.uuid4())


def write_config(tmp_path: Path, body: str = "") -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n' + body,
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return write_config(tmp_path, '[context]\nmode = "enforce"\nmodel_context_window = 393216\n'
                                 "model_window_verified = true\n")


async def _new_session(session: AsyncSession, cfg: AppConfig) -> uuid.UUID:
    created = await create_session(session)
    return uuid.UUID(created["session_id"])


async def _seed_user_message(session: AsyncSession, session_id: uuid.UUID,
                             content: str) -> str:
    """来源校验（v2 §2.1）要求增量落到真实用户消息上。"""
    from xhs_agent.db.models import ChatMessage
    from xhs_agent.services.chat import append_message

    await append_message(session, session_id, role="user", content=content)
    row = (
        await session.execute(
            select(ChatMessage.id)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    return str(row)


async def _seed_user_message(session: AsyncSession, session_id: uuid.UUID,
                             content: str) -> str:
    """来源校验（v2 §2.1）要求增量落到真实用户消息上。"""
    from xhs_agent.db.models import ChatMessage
    from xhs_agent.services.chat import append_message

    await append_message(session, session_id, role="user", content=content)
    row = (
        await session.execute(
            select(ChatMessage.id)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    return str(row)


def _delta(operations: list[dict[str, object]] | None = None,
           proposals: list[dict[str, object]] | None = None) -> ContextDelta:
    return ContextDelta.from_dict({"operations": operations or [],
                                   "proposals": proposals or []})


class TestContextState:
    async def test_delta_is_persisted_with_audit(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        stored = await context_state.load(db_session, session_id)
        assert stored is not None and stored.state_version == 0

        message_id = await _seed_user_message(db_session, session_id, "文案 300 字以内")
        applied = await context_state.apply_delta(
            db_session, cfg, stored,
            _delta([{"op": "add", "kind": "constraint", "value": "300 字以内",
                     "evidence": "300 字以内"}]),
            message_id=message_id, user_text="文案 300 字以内", mode="enforce",
        )
        assert applied.applied and applied.version == 1

        reloaded = await context_state.load(db_session, session_id)
        assert reloaded is not None
        assert reloaded.state_version == 1
        assert reloaded.snapshot.facts[0].value == "300 字以内"
        events = list(
            (await db_session.execute(
                select(ChatContextEvent).where(
                    ChatContextEvent.session_id == session_id
                )
            )).scalars().all()
        )
        assert len(events) == 1 and events[0].state_version == 1
        assert events[0].event_type == "delta" and events[0].actor == "extract"

    async def test_stale_version_does_not_advance_state(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        stored = await context_state.load(db_session, session_id)
        assert stored is not None
        message_id = await _seed_user_message(db_session, session_id, "蹭热点")
        await context_state.apply_delta(
            db_session, cfg, stored,
            _delta([{"op": "add", "kind": "goal", "value": "蹭热点", "evidence": "蹭热点"}]),
            message_id=message_id, user_text="蹭热点", mode="enforce",
        )
        # 用旧快照（version=0）再应用一次：CAS 失败，不覆盖新状态
        stale = await context_state.apply_delta(
            db_session, cfg, stored,
            _delta([{"op": "add", "kind": "preference", "value": "口语",
                     "evidence": "口语"}]),
            message_id=message_id, user_text="口语", mode="enforce",
        )
        assert stale.applied is False and stale.conflict is True
        latest = await context_state.load(db_session, session_id)
        assert latest is not None and latest.state_version == 1
        assert [fact.value for fact in latest.snapshot.facts] == ["蹭热点"]

    async def test_shadow_mode_writes_nothing(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        stored = await context_state.load(db_session, session_id)
        assert stored is not None
        message_id = await _seed_user_message(db_session, session_id, "蹭热点")
        result = await context_state.apply_delta(
            db_session, cfg, stored,
            _delta([{"op": "add", "kind": "goal", "value": "蹭热点", "evidence": "蹭热点"}]),
            message_id=message_id, user_text="蹭热点", mode="shadow",
        )
        assert result.applied is False and result.reason == "shadow_mode"
        latest = await context_state.load(db_session, session_id)
        assert latest is not None and latest.state_version == 0
        assert latest.snapshot.facts == []

    async def test_action_conflict_returns_current_version(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        with pytest.raises(ConflictError) as caught:
            await context_state.perform_action(
                db_session, session_id, action="reset_topic", target_id="",
                value="", expected_state_version=5,
            )
        assert caught.value.detail["state_version"] == 0

    async def test_confirm_proposal_and_reset_topic(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        stored = await context_state.load(db_session, session_id)
        assert stored is not None
        message_id = await _seed_user_message(db_session, session_id, "随便改改")
        await context_state.apply_delta(
            db_session, cfg, stored,
            _delta(proposals=[{"kind": "preference", "value": "更口语",
                               "reason": "inferred"}]),
            message_id=message_id, user_text="随便改改", mode="enforce",
        )
        latest = await context_state.load(db_session, session_id)
        assert latest is not None and latest.snapshot.proposals
        proposal_id = latest.snapshot.proposals[0].proposal_id

        confirmed = await context_state.perform_action(
            db_session, session_id, action="confirm", target_id=proposal_id,
            value="", expected_state_version=latest.state_version,
        )
        assert confirmed["decision"] == "applied"
        after = await context_state.load(db_session, session_id)
        assert after is not None
        assert after.snapshot.proposals == []
        assert after.snapshot.facts[0].value == "更口语"

        await context_state.perform_action(
            db_session, session_id, action="reset_topic", target_id="", value="",
            expected_state_version=after.state_version,
        )
        reset = await context_state.load(db_session, session_id)
        assert reset is not None
        assert reset.topic_id != latest.topic_id
        assert reset.snapshot.active() == []  # topic 作用域被归档

    async def test_invalid_evidence_is_rejected(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        stored = await context_state.load(db_session, session_id)
        assert stored is not None
        message_id = await _seed_user_message(db_session, session_id, "帮我写一版文案")
        result = await context_state.apply_delta(
            db_session, cfg, stored,
            _delta([{"op": "add", "kind": "goal", "value": "蹭热点",
                     "evidence": "用户想蹭热点"}]),
            message_id=message_id, user_text="帮我写一版文案", mode="enforce",
        )
        assert result.applied is False
        assert result.decisions[0]["reason"] == "evidence_mismatch"


class TestArtifactVersions:
    async def test_version_chain_and_replay(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        initial = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1,
            content={"draft": {"body": "初稿"}},
        )
        assert initial.version_no == 1

        same = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1,
            content={"draft": {"body": "不应覆盖"}},
        )
        assert same.id == initial.id and same.content == {"draft": {"body": "初稿"}}

        req = artifact_versions.request_key(session_id, key, turn_id="t1", call_id="c1",
                                            request="更短")
        claim = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, request_key_value=req,
            base_version_id=initial.id,
        )
        assert claim.is_new and claim.status == "pending"
        committed = await artifact_versions.commit_success(
            db_session, claim_row=claim.row, content={"revised": {"body": "短稿"}},
            prompt_versions={"copy_revise": 1}, usage={"cost_cny": 0.01},
            expected_state_version=0,
        )
        assert committed.version_no == 2

        replay = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, request_key_value=req,
            base_version_id=initial.id,
        )
        assert replay.is_new is False and replay.status == "succeeded"
        assert replay.row is not None and replay.row.version_no == 2

        head = await artifact_versions.head(db_session, session_id, key)
        assert head is not None and head.id == committed.id

    async def test_failed_claim_does_not_consume_version(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        initial = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1,
            content={"draft": {"body": "初稿"}},
        )
        req = artifact_versions.request_key(session_id, key, turn_id="t1", call_id="c1",
                                            request="失败")
        claim = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, request_key_value=req,
            base_version_id=initial.id,
        )
        await artifact_versions.finish(db_session, claim.row, status="failed",
                                       error="上游失败")

        ok_req = artifact_versions.request_key(session_id, key, turn_id="t1", call_id="c2",
                                               request="成功")
        second = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, request_key_value=ok_req,
            base_version_id=initial.id,
        )
        committed = await artifact_versions.commit_success(
            db_session, claim_row=second.row, content={"revised": {"body": "新版"}},
            expected_state_version=0,
        )
        assert committed.version_no == 2  # 失败记录没有占号

    async def test_state_version_conflict_blocks_commit(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        initial = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1,
            content={"draft": {"body": "初稿"}},
        )
        req = artifact_versions.request_key(session_id, key, turn_id="t1", call_id="c1",
                                            request="更短")
        claim = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, request_key_value=req,
            base_version_id=initial.id,
        )
        with pytest.raises(ConflictError):
            await artifact_versions.commit_success(
                db_session, claim_row=claim.row, content={"revised": {"body": "x"}},
                expected_state_version=9,
            )
        head = await artifact_versions.head(db_session, session_id, key)
        assert head is not None and head.id == initial.id  # head 没被覆盖

    async def test_cross_session_version_is_not_visible(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        first = await _new_session(db_session, cfg)
        second = await _new_session(db_session, cfg)
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        initial = await artifact_versions.ensure_initial(
            db_session, session_id=first, object_key_value=key, object_type="draft",
            run_id=RUN_ID, position=1, content={"draft": {"body": "初稿"}},
        )
        assert await artifact_versions.get_version(db_session, second, initial.id) is None
        assert await artifact_versions.list_versions(db_session, second) == []


class TestImageCache:
    async def test_store_hit_expire_and_bypass(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        identity = image_cache.build_identity(cfg, ["a" * 64])
        assert await image_cache.store(
            db_session, cfg, session_id=session_id, identity=identity,
            payload={"clue": {"hotspot_raw": "描述", "why_it_works": ["反差"],
                     "mechanisms": [{"name": "反差"}],
                     "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9,
                                   "confidence": 0.9, "evidence": "夜跑"}]},
                     "raw_text": "描述"}, turn_id="t1",
        )
        hit = await image_cache.lookup(
            db_session, cfg, session_id=session_id, identity=identity, turn_id="t2",
        )
        assert hit.outcome == "hit" and hit.level == "persistent"
        assert hit.payload and hit.payload["raw_text"] == "描述"
        assert hit.payload["clue"]["elements"][0]["value"] == "夜跑"

        # 同键重复写入只保留一条
        await image_cache.store(
            db_session, cfg, session_id=session_id, identity=identity,
            payload={"clue": {}, "raw_text": "覆盖"}, turn_id="t3",
        )
        count = int(
            (await db_session.execute(
                select(func.count()).select_from(ChatImageParseCache)
            )).scalar_one()
        )
        assert count == 1

        # 到期 → miss（命中不续期）
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update

        past = datetime.now(UTC) - timedelta(days=2)
        await db_session.execute(
            update(ChatImageParseCache).values(
                created_at=past, expires_at=past + timedelta(days=1)
            )
        )
        await db_session.commit()
        expired = await image_cache.lookup(
            db_session, cfg, session_id=session_id, identity=identity, turn_id="t4",
        )
        assert expired.outcome == "miss" and expired.reason == "expired"

    async def test_disabled_cache_is_bypass(
        self, db_session: AsyncSession, tmp_path: Path
    ) -> None:
        cfg = write_config(tmp_path, "[context]\nimage_cache_enabled = false\n")
        session_id = await _new_session(db_session, cfg)
        identity = image_cache.build_identity(cfg, ["a" * 64])
        result = await image_cache.lookup(
            db_session, cfg, session_id=session_id, identity=identity, turn_id="t1",
        )
        assert result.outcome == "bypass" and result.reason == "disabled"

    async def test_cross_session_isolation(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        first = await _new_session(db_session, cfg)
        second = await _new_session(db_session, cfg)
        identity = image_cache.build_identity(cfg, ["a" * 64])
        await image_cache.store(db_session, cfg, session_id=first, identity=identity,
                                payload={"clue": {"raw_text": "x"}, "raw_text": "x"})
        other = await image_cache.lookup(
            db_session, cfg, session_id=second, identity=identity, turn_id="t",
        )
        assert other.outcome == "miss"


class TestObservations:
    async def test_duplicate_event_id_is_written_once(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        first = await record_observation(
            db_session, event_type="context_assembled", session_id=session_id,
            key="t1:1", payload={"input_est": 10},
        )
        second = await record_observation(
            db_session, event_type="context_assembled", session_id=session_id,
            key="t1:1", payload={"input_est": 10},
        )
        assert first is True and second is False
        rows = list((await db_session.execute(select(ContextObservation))).scalars().all())
        assert len(rows) == 1

    async def test_payload_is_sanitized(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = await _new_session(db_session, cfg)
        await record_observation(
            db_session, event_type="cache_lookup_finished", session_id=session_id,
            key="t1:1", payload={"outcome": "hit", "user_text": "原文",
                                 "args": {"path": "x"}},
        )
        row = (await db_session.execute(select(ContextObservation))).scalars().one()
        assert "user_text" not in row.payload and "args" not in row.payload
        assert row.payload["outcome"] == "hit"

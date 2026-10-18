"""E7 第六批修复的**真库 / HTTP 复现**（v2 §4.1.1，case_id 对应问题清单）。

需要真实 Postgres（`pytest -m integration`）：
- CTX01：单独撤回真实落库（HTTP 读回 retracted、重复撤回不推进版本）；
- CTX03：CAS 竞争返回 409（不能以 200 包装成 noop）；
- CTX04：确认替换旧项已变 → 409（无部分修改）；
- CTX05：旧会话（有用户历史、无恢复记录）GET context 返回 `recovery.status=pending`；
- CTX06/07：版本链逐次承接、旧版分支成功、生成期间 head 变化 → 409 且无 `MissingGreenlet`。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.api.deps import get_config, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.errors import ConflictError
from xhs_agent.db import Base
from xhs_agent.db.models import ChatSession
from xhs_agent.services import artifact_versions, context_state
from xhs_agent.services.chat import append_message, create_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

RUN_ID = str(uuid.uuid4())


def write_config(tmp_path: Path) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nruns_dir = "{(tmp_path / "runs").as_posix()}"\n'
        '[context]\n'
        'mode = "enforce"\n'
        'model_context_window = 393216\n'
        'model_window_verified = true\n',
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


@pytest_asyncio.fixture
async def client(db_engine: AsyncEngine, cfg: AppConfig) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def _session() -> AsyncIterator[AsyncSession]:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_fact(
    db_session: AsyncSession, cfg: AppConfig, session_id: uuid.UUID, *,
    value: str = "300 字以内", user_text: str = "控制在 300 字以内",
) -> uuid.UUID:
    """真实走一次抽取应用（含 CAS 与审计），返回事实 id。"""
    await append_message(db_session, session_id, role="user", content=user_text)
    from xhs_agent.db.models import ChatMessage

    row_id = (
        await db_session.execute(
            select(ChatMessage.id)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    stored = await context_state.load(db_session, session_id)
    assert stored is not None
    delta = context_state.ContextDelta.from_dict(
        {"operations": [{"op": "add", "kind": "constraint", "value": value,
                         "evidence": value}]}
    )
    applied = await context_state.apply_delta(
        db_session, cfg, stored, delta,
        message_id=str(row_id), user_text=user_text, mode="enforce",
    )
    assert applied.applied and applied.snapshot is not None
    return applied.snapshot.facts[0].fact_id


class TestCTX01RetractHttp:
    @pytest.mark.asyncio
    async def test_retract_persists_and_repeat_is_noop(
        self, client: httpx.AsyncClient, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        created = await client.post("/api/chat/sessions")
        assert created.status_code == 201, created.text
        session_id = str(created.json()["session_id"])
        fact_id = await _seed_fact(db_session, cfg, uuid.UUID(session_id))

        first = await client.post(
            f"/api/chat/sessions/{session_id}/context/actions",
            json={"action": "retract", "target_id": str(fact_id),
                  "expected_state_version": 1},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["decision"] == "applied"
        retracted = [f for f in body["context"]["facts"] if f["fact_id"] == str(fact_id)]
        assert retracted and retracted[0]["status"] == "retracted"

        # 重复撤回：noop，不再推进版本（缺陷时会再 +1）
        second = await client.post(
            f"/api/chat/sessions/{session_id}/context/actions",
            json={"action": "retract", "target_id": str(fact_id),
                  "expected_state_version": body["context"]["state_version"]},
        )
        assert second.status_code == 200
        assert second.json()["decision"] == "noop"
        assert second.json()["context"]["state_version"] == body["context"]["state_version"]


class TestCTX03CasConflictHttp:
    @pytest.mark.asyncio
    async def test_stale_version_returns_409(
        self, client: httpx.AsyncClient, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        created = await client.post("/api/chat/sessions")
        session_id = str(created.json()["session_id"])
        fact_id = await _seed_fact(db_session, cfg, uuid.UUID(session_id))
        # 用过期版本提交动作：409（缺陷时 CAS 失败被折成 200 noop）
        response = await client.post(
            f"/api/chat/sessions/{session_id}/context/actions",
            json={"action": "retract", "target_id": str(fact_id),
                  "expected_state_version": 99},
        )
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["reason"] == "state_version_conflict"
        assert detail["retryable"] is True


class TestCTX04ConfirmReplaceHttp:
    @pytest.mark.asyncio
    async def test_confirm_replace_409_when_target_changed(
        self, client: httpx.AsyncClient, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        created = await client.post("/api/chat/sessions")
        session_id = str(created.json()["session_id"])
        session_uuid = uuid.UUID(session_id)
        old_fact = await _seed_fact(db_session, cfg, session_uuid)
        stored = await context_state.load(db_session, session_uuid)
        assert stored is not None
        await context_state.apply_delta(
            db_session, cfg, stored,
            context_state.ContextDelta.from_dict(
                {"operations": [{"op": "add", "kind": "constraint",
                                 "value": "100 字以内", "evidence": "100 字以内"}]}
            ),
            message_id=str(stored.snapshot.facts[0].source_ref.get("message_id")),
            user_text="改成 100 字以内", mode="enforce",
        )
        stored2 = await context_state.load(db_session, session_uuid)
        assert stored2 is not None
        proposal = next(p for p in stored2.snapshot.proposals if p.reason == "conflict")
        retract = await client.post(
            f"/api/chat/sessions/{session_id}/context/actions",
            json={"action": "retract", "target_id": str(old_fact),
                  "expected_state_version": stored2.state_version},
        )
        assert retract.status_code == 200
        latest = retract.json()["context"]
        confirm = await client.post(
            f"/api/chat/sessions/{session_id}/context/actions",
            json={"action": "confirm", "target_id": proposal.proposal_id,
                  "expected_state_version": latest["state_version"]},
        )
        assert confirm.status_code == 409, confirm.text
        assert confirm.json()["detail"]["reason"] == "replace_target_changed"


class TestA01NewSessionReady:
    @pytest.mark.asyncio
    async def test_new_session_first_message_is_ready(
        self, client: httpx.AsyncClient, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        """A01：新建会话在创建事务里写入维护起点 → 首条消息后仍 complete。"""
        created = await client.post("/api/chat/sessions")
        session_id = str(created.json()["session_id"])
        await append_message(db_session, uuid.UUID(session_id), role="user",
                             content="以后别提明星姓名")
        response = await client.get(f"/api/chat/sessions/{session_id}/context")
        assert response.status_code == 200
        recovery = response.json()["recovery"]
        assert recovery["status"] == "complete"          # 缺陷时为 pending/legacy_unrecovered
        assert recovery["baseline_created_at"]           # 明确维护起点
        assert response.json()["recovery_status"] == "complete"


class TestCTX05LegacySessionPending:
    @pytest.mark.asyncio
    async def test_legacy_session_reports_pending_recovery(
        self, client: httpx.AsyncClient, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        created = await client.post("/api/chat/sessions")
        session_id = str(created.json()["session_id"])
        await append_message(db_session, uuid.UUID(session_id), role="user",
                             content="以后别提明星姓名")
        # 真正的旧会话：有用户历史但没有恢复记录（模拟 A01 之前建的行）→ pending
        await db_session.execute(
            update(ChatSession)
            .where(ChatSession.id == uuid.UUID(session_id))
            .values(context_state={"facts": [], "proposals": [], "object_refs": [],
                                   "archived": []})
        )
        await db_session.commit()
        response = await client.get(f"/api/chat/sessions/{session_id}/context")
        assert response.status_code == 200
        recovery = response.json()["recovery"]
        assert recovery["status"] == "pending"
        assert response.json()["recovery_status"] == "partial"


class TestCTX06And07VersionChain:
    @pytest.mark.asyncio
    async def test_three_revisions_use_last_success_and_old_branch_allowed(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = uuid.UUID(str((await create_session(db_session))["session_id"]))
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        v1 = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, content=_draft("D0"),
        )
        v2 = await _commit(db_session, session_id, key, base=v1, content=_draft("D1"))
        assert v2.version_no == 2 and v2.expected_head_version_id == v1.id
        v3 = await _commit(db_session, session_id, key, base=v2, content=_draft("D2"))
        assert v3.version_no == 3
        # 显式从 v1 分支：head=v3、base=v1 → 允许（E7CTX-07），产出 v4
        v4 = await _commit(db_session, session_id, key, base=v1, content=_draft("D3"))
        assert v4.version_no == 4 and v4.base_version_id == v1.id

    @pytest.mark.asyncio
    async def test_head_changed_during_generation_conflicts_without_greenlet_error(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = uuid.UUID(str((await create_session(db_session))["session_id"]))
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        v1 = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, content=_draft("D0"),
        )
        stale_claim = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1,
            request_key_value=artifact_versions.request_key(
                session_id, key, turn_id="t", call_id="c1", request="更短"),
            base_version_id=v1.id, expected_head_version_id=v1.id,
            expected_state_version=0,
        )
        assert stale_claim.row is not None
        winner = await _commit(db_session, session_id, key, base=v1, content=_draft("D1"))
        winner_id = str(winner.id)   # 旧认领提交的 rollback 会让 ORM 对象过期，先取标量
        with pytest.raises(ConflictError) as caught:
            await artifact_versions.commit_success(
                db_session, claim_row=stale_claim.row, content=_draft("D9"),
            )
        assert caught.value.detail["reason"] == "head_changed_during_generation"
        assert caught.value.detail["head_version_id"] == winner_id

    @pytest.mark.asyncio
    async def test_replay_returns_original_base_reference(
        self, db_session: AsyncSession, cfg: AppConfig
    ) -> None:
        session_id = uuid.UUID(str((await create_session(db_session))["session_id"]))
        key = artifact_versions.object_key("draft", RUN_ID, 1)
        v1 = await artifact_versions.ensure_initial(
            db_session, session_id=session_id, object_key_value=key,
            object_type="draft", run_id=RUN_ID, position=1, content=_draft("D0"),
        )
        req = artifact_versions.request_key(session_id, key, turn_id="t", call_id="c",
                                            request="更短")
        claim = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key, object_type="draft",
            run_id=RUN_ID, position=1, request_key_value=req, base_version_id=v1.id,
            expected_head_version_id=v1.id, expected_state_version=0,
        )
        assert claim.row is not None
        committed = await artifact_versions.commit_success(
            db_session, claim_row=claim.row, content=_draft("D1"),
        )
        await _commit(db_session, session_id, key, base=committed, content=_draft("D2"))
        replay = await artifact_versions.claim(
            db_session, session_id=session_id, object_key_value=key, object_type="draft",
            run_id=RUN_ID, position=1, request_key_value=req, base_version_id=v1.id,
        )
        assert replay.is_new is False and replay.status == "succeeded"
        assert replay.row is not None
        # 重放返回**认领时保存的原基础引用**（缺陷时用当前 head 重新拼）
        assert replay.row.base_version_id == v1.id
        assert replay.row.version_no == 2


def _draft(body: str) -> dict[str, Any]:
    return {
        "titles": [{"text": f"标题（{body}）", "style": "直给"}],
        "body": body,
        "tags": ["#测试"],
        "cover_text": "",
        "first_3s": "",
        "shot_list": [],
        "compliance_notes": [],
    }


async def _commit(
    db_session: AsyncSession,
    session_id: uuid.UUID,
    key: str,
    *,
    base: artifact_versions.ChatArtifactVersion,
    content: dict[str, Any],
) -> artifact_versions.ChatArtifactVersion:
    head = await artifact_versions.head(db_session, session_id, key)
    claim = await artifact_versions.claim(
        db_session, session_id=session_id, object_key_value=key, object_type="draft",
        run_id=RUN_ID, position=1,
        request_key_value=artifact_versions.request_key(
            session_id, key, turn_id=f"t-{uuid.uuid4().hex[:8]}",
            call_id=f"c-{uuid.uuid4().hex[:8]}", request=str(content.get("body", ""))),
        base_version_id=base.id,
        expected_head_version_id=head.id if head is not None else None,
        expected_state_version=0,
    )
    assert claim.is_new and claim.row is not None
    return await artifact_versions.commit_success(
        db_session, claim_row=claim.row, content=content, expected_state_version=0,
    )

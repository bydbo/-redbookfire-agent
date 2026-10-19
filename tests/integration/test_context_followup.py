"""2c6f558 独立验收反例：HTTP、真实取消 / 竞争、版本身份与约束消费。"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xhs_agent.api.deps import get_config, get_session
from xhs_agent.api.main import create_app
from xhs_agent.config import AppConfig, load_config
from xhs_agent.core.context_policy import TurnContextPolicy
from xhs_agent.core.errors import ConflictError, DependencyUnavailableError
from xhs_agent.db import Base
from xhs_agent.db.models import ChatContextAction, ChatMessage, ChatSession
from xhs_agent.schemas import ContextFact, HotspotClue
from xhs_agent.services import artifact_versions, chat_tools, context_state
from xhs_agent.services.chat import append_message, create_session, save_attachment
from xhs_agent.services.chat_turns import _load_source_binding, _prepare_context
from xhs_agent.services.image_clue import ImageClueParse
from xhs_agent.tools.llm import BaseProvider, LLMResult, StructuredCaller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    path = tmp_path / "config.toml"
    path.write_text(f'[paths]\nruns_dir="{tmp_path.as_posix()}/runs"\n'
                    '[context]\nmode="enforce"\nmodel_context_window=393216\n'
                    'model_window_verified=true\n', encoding="utf-8")
    return load_config(str(path))


class FixedProvider(BaseProvider):
    name = "fixed-local"

    def __init__(self, *, started: asyncio.Event | None = None,
                 gate: asyncio.Event | None = None, payload: Any = None) -> None:
        super().__init__(model="fixed-local")
        self.started, self.gate, self.payload = started, gate, payload
        self.calls = 0

    async def complete(self, call: Any) -> LLMResult:
        self.calls += 1
        if self.started:
            self.started.set()
        if self.gate:
            await self.gate.wait()
        payload = self.payload(call, self.calls) if callable(self.payload) else self.payload
        return LLMResult(text=json.dumps(payload or {"operations": [], "proposals": []}),
                         provider=self.name, model=self.model, prompt_tokens=100,
                         completion_tokens=20, cost_cny=0.01, usage_status="actual")


async def legacy(db: AsyncSession, count: int = 1) -> uuid.UUID:
    row = ChatSession(context_state={})
    db.add(row)
    await db.flush()
    for i in range(count):
        db.add(ChatMessage(session_id=row.id, role="user", status="succeeded",
                           content=f"限制内容{i:02}", created_at=datetime(2020, 1, 1, tzinfo=UTC)))
    await db.commit()
    return row.id


async def recover(db: AsyncSession, cfg: AppConfig, sid: uuid.UUID,
                  provider: FixedProvider, *, action_id: str | None = None,
                  expected: int = 0, recovery_id: str | None = None,
                  retry_of: str | None = None) -> dict[str, Any]:
    return await context_state.perform_action(
        db, sid, action="recover", target_id="", value="", expected_state_version=expected,
        action_id=action_id or str(uuid.uuid4()), recovery_id=recovery_id, retry_of=retry_of,
        cfg=cfg, caller=StructuredCaller(provider=provider),
    )


def clue(raw: str = "夜跑") -> HotspotClue:
    return HotspotClue.from_dict({"hotspot_raw": raw, "why_it_works": ["反差"],
        "mechanisms": [{"name": "反差", "explain": "夜里跑步"}],
        "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9,
                      "confidence": 0.9, "evidence": "夜跑"}], "match_keywords": ["夜跑"]})


async def test_clue_http_success_and_idempotent_replay(
    db_engine: AsyncEngine, db_session: AsyncSession, cfg: AppConfig,
) -> None:
    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    key = "clue:images:" + "a" * 64
    content = {"raw_text": "夜跑", "clue": clue().to_dict(), "edited_fields": []}
    # 原解析时间故意早于本次保存，稳定暴露反序列化刷新元数据的跨秒问题。
    content["clue"].update(created_at="2000-01-01T00:00:00+00:00", provider="fixture", model="original")
    v1 = await artifact_versions.ensure_initial(db, session_id=sid, object_key_value=key,
        object_type="clue", run_id=None, position=None, content=content)
    payload = {"action_id": str(uuid.uuid4()), "expected_state_version": 0,
               "expected_head_version_id": str(v1.id), "base_version_id": str(v1.id),
               "object_key": key, "content": content}
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def session_dep():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session_dep
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/api/chat/sessions/{sid}/artifacts/clue-versions", json=payload)
        assert first.status_code == 201, first.text
        assert first.json()["action"] == "edit_clue"
        assert first.json()["version"]["content"] == content
        replay = await client.post(f"/api/chat/sessions/{sid}/artifacts/clue-versions", json=payload)
        assert replay.status_code == 200 and replay.json()["replay"] is True
        assert replay.json()["version_id"] == first.json()["version_id"]
        assert replay.json()["state_version"] == first.json()["state_version"]
        changed = {**payload, "base_version_id": first.json()["version_id"]}
        conflict = await client.post(f"/api/chat/sessions/{sid}/artifacts/clue-versions", json=changed)
        assert conflict.status_code == 409
        selected = await client.post(f"/api/chat/sessions/{sid}/artifacts/{v1.id}/select", json={
            "action_id": str(uuid.uuid4()), "expected_state_version": first.json()["state_version"]})
        assert selected.status_code == 200, selected.text
        assert selected.json()["version_id"] == str(v1.id)
        assert selected.json()["action"] == "select_clue"


async def test_recovery_stale_version_has_zero_model_calls(db_session: AsyncSession, cfg: AppConfig):
    sid = await legacy(db_session)
    provider = FixedProvider()
    with pytest.raises(ConflictError, match="上下文状态已变化"):
        await recover(db_session, cfg, sid, provider, expected=999)
    assert provider.calls == 0


async def test_21_same_timestamp_empty_pages_and_cost(db_session: AsyncSession, cfg: AppConfig):
    sid = await legacy(db_session, 21)
    provider = FixedProvider()
    first = await recover(db_session, cfg, sid, provider)
    state = await context_state.load(db_session, sid)
    assert state and state.recovery["scanned_user_messages"] == 20
    second = await recover(db_session, cfg, sid, provider, expected=state.state_version,
                           recovery_id=state.recovery["recovery_id"])
    assert first["status"] == second["status"] == "succeeded" and provider.calls == 2
    state = await context_state.load(db_session, sid)
    assert state and state.recovery["scanned_user_messages"] == 21
    account = await db_session.get(ChatMessage, uuid.UUID(state.recovery["accounting_message_id"]))
    assert account and (account.prompt_tokens, account.completion_tokens) == (100, 20)
    assert float(account.cost_cny) == pytest.approx(0.01)
    assert account.progress["recovery_usage"]["unknown_usage"] is False
    await context_state.perform_action(db_session, sid, action="complete_recovery", target_id="", value="",
        expected_state_version=state.state_version, action_id=str(uuid.uuid4()),
        recovery_id=state.recovery["recovery_id"])
    assert (await context_state.load(db_session, sid)).recovery["status"] == "complete"


async def test_actual_cancel_then_explicit_retry(db_session: AsyncSession, cfg: AppConfig):
    sid = await legacy(db_session)
    started, gate = asyncio.Event(), asyncio.Event()
    old_id = str(uuid.uuid4())
    provider = FixedProvider(started=started, gate=gate)
    task = asyncio.create_task(recover(db_session, cfg, sid, provider, action_id=old_id))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = await context_state.load(db_session, sid)
    assert state and state.recovery["status"] == "interrupted"
    assert state.recovery["scanned_user_messages"] == 0
    account = await db_session.get(ChatMessage, uuid.UUID(state.recovery["accounting_message_id"]))
    assert account and account.status == "interrupted"
    assert account.progress["recovery_usage"]["unknown_usage"] is True
    retry = await recover(db_session, cfg, sid, FixedProvider(), expected=state.state_version,
                          recovery_id=state.recovery["recovery_id"], retry_of=old_id)
    assert retry["status"] == "succeeded"


async def test_stale_claim_checks_owned_page(db_session: AsyncSession, cfg: AppConfig):
    sid = await legacy(db_session)
    old_id = uuid.uuid4()
    chat = await db_session.get(ChatSession, sid)
    chat.context_state = {"recovery": {"status": "running", "page_action_id": str(old_id),
        "recovery_id": str(uuid.uuid4())}}
    db_session.add(ChatContextAction(session_id=sid, action_id=old_id, action="recover", status="running",
        updated_at=datetime.now(UTC) - timedelta(days=2)))
    await db_session.commit()
    provider = FixedProvider()
    result = await recover(db_session, cfg, sid, provider)
    assert result["reason"] == "recovery_running" and provider.calls == 0
    assert (await context_state.load(db_session, sid)).recovery["status"] == "interrupted"
    row = (await db_session.execute(select(ChatContextAction).where(
        ChatContextAction.action_id == old_id))).scalar_one()
    assert row.status == "interrupted"


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
async def test_scoped_constraints_and_partial_protection(db_session: AsyncSession, cfg: AppConfig, mode: str):
    sid = uuid.UUID((await create_session(db_session))["session_id"])
    chat = await db_session.get(ChatSession, sid)
    state = dict(chat.context_state)
    state["object_refs"] = [{"object_key": f"draft:object{x}:1", "object_type": "draft"} for x in "AB"]
    state["facts"] = [ContextFact(fact_id="f" + obj, kind="constraint", value="ONLY " + obj,
        scope="artifact", scope_id=f"draft:object{obj}:1", status="active", evidence="ONLY " + obj,
        source_ref={"message_id": "fixture"}, created_version=1).to_dict() for obj in "AB"]
    state["recovery"] = {"status": "pending", "reason": "legacy_unrecovered"}
    chat.context_state = state
    await db_session.commit()
    policy = TurnContextPolicy(mode=mode)
    ctx = chat_tools.ToolContext(cfg=cfg, session=db_session, session_id=sid, policy=policy)
    constraints = await chat_tools._constraints_for(ctx, object_key="draft:objectA:1")
    assert [c["value"] for c in constraints] == (["ONLY A"] if mode == "enforce" else [])
    prep = await _prepare_context(db_session, cfg, provider=FixedProvider(), session_id=sid,
        user_message_id=uuid.uuid4(), user_text="继续改稿", turn_id=str(uuid.uuid4()), ledger={}, policy=policy)
    assert prep.status == "confirmation_required" and prep.blocking_reply


async def test_source_binding_is_scoped_and_read_failure_stops(db_session: AsyncSession):
    sid = uuid.UUID((await create_session(db_session))["session_id"])
    message = await append_message(db_session, sid, role="user", content="改这一版",
                                   operation_binding={"operation": "revise_draft"})
    assert await _load_source_binding(db_session, uuid.UUID(message["message_id"]), session_id=sid)
    with pytest.raises(DependencyUnavailableError):
        await _load_source_binding(db_session, uuid.UUID(message["message_id"]), session_id=uuid.uuid4())
    fake = AsyncMock()
    fake.execute.side_effect = RuntimeError("read failed")
    with pytest.raises(DependencyUnavailableError):
        await _load_source_binding(fake, uuid.uuid4(), session_id=sid)


async def test_fresh_image_result_keeps_matching_version(
    db_session: AsyncSession, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch,
):
    sid = uuid.UUID((await create_session(db_session))["session_id"])
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yN4sAAAAASUVORK5CYII=")
    attachment = save_attachment(cfg, sid, image, name="fixture.png", mime="image/png")
    fake = AsyncMock(side_effect=[ImageClueParse(raw_text=raw, clue=clue(raw),
        prompt_versions={"image_hotspot_clue": 1}) for raw in ("C0", "C2")])
    monkeypatch.setattr(chat_tools, "parse_image_clue", fake)

    def ctx():
        return chat_tools.ToolContext(cfg=cfg, session=db_session, session_id=sid,
            policy=TurnContextPolicy(mode="enforce", image_cache_enabled=False), turn_id=str(uuid.uuid4()))

    first = await chat_tools._image_clue_for(ctx(), [attachment])
    cfg.vision.model += "-changed"
    second = await chat_tools._image_clue_for(ctx(), [attachment])
    assert first.version_id != second.version_id and second.raw_text == "C2"
    saved = await artifact_versions.get_version(db_session, sid, uuid.UUID(second.version_id))
    assert saved and saved.content["raw_text"] == second.raw_text and fake.call_count == 2


async def test_clue_edit_failure_rolls_back_entire_business_state(
    db_session: AsyncSession, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch,
):
    sid = uuid.UUID((await create_session(db_session))["session_id"])
    key = "clue:images:" + "a" * 64
    content = {"raw_text": "夜跑", "clue": clue().to_dict(), "edited_fields": []}
    original = await artifact_versions.ensure_initial(db_session, session_id=sid,
        object_key_value=key, object_type="clue", run_id=None, position=None, content=content)
    original_id = original.id

    async def fault(*_args, **_kwargs):
        raise RuntimeError("injected before state commit")

    monkeypatch.setattr(context_state, "_commit", fault)
    action_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        await context_state.edit_clue_version(db_session, sid, cfg, action_id=action_id,
            expected_state_version=0, expected_head_version_id=str(original_id),
            base_version_id=str(original_id), object_key=key, content=content)
    state = await context_state.load(db_session, sid)
    assert state and state.state_version == 0 and not state.snapshot.object_refs
    assert (await artifact_versions.head(db_session, sid, key)).id == original_id
    versions = await artifact_versions.list_versions(db_session, sid, object_key_value=key)
    assert len([row for row in versions if row.status == "succeeded"]) == 1
    action = (await db_session.execute(select(ChatContextAction).where(
        ChatContextAction.action_id == uuid.UUID(action_id)))).scalar_one()
    assert action.status == "failed"


async def test_two_recovery_actions_send_only_one_page(
    db_engine: AsyncEngine, db_session: AsyncSession, cfg: AppConfig,
):
    sid = await legacy(db_session)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    started, gate = asyncio.Event(), asyncio.Event()
    first_provider = FixedProvider(started=started, gate=gate)

    async def first():
        async with factory() as db:
            return await recover(db, cfg, sid, first_provider)

    task = asyncio.create_task(first())
    await asyncio.wait_for(started.wait(), 5)
    second_provider = FixedProvider()
    async with factory() as db:
        result = await recover(db, cfg, sid, second_provider)
        assert result["reason"] == "recovery_running" and second_provider.calls == 0
    gate.set()
    assert (await task)["status"] == "succeeded"
    assert first_provider.calls == 1


async def test_state_changed_during_page_does_not_commit_candidates(
    db_engine: AsyncEngine, db_session: AsyncSession, cfg: AppConfig,
):
    sid = await legacy(db_session)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    started, gate = asyncio.Event(), asyncio.Event()
    provider = FixedProvider(started=started, gate=gate)

    async def execute():
        async with factory() as db:
            return await recover(db, cfg, sid, provider)

    task = asyncio.create_task(execute())
    await asyncio.wait_for(started.wait(), 5)
    async with factory() as other:
        await other.execute(update(ChatSession).where(ChatSession.id == sid).values(state_version=1))
        await other.commit()
    gate.set()
    result = await task
    assert result["decision"] == "conflict"
    state = await context_state.load(db_session, sid)
    assert state and state.state_version == 1 and state.recovery["scanned_user_messages"] == 0
    account = await db_session.get(ChatMessage, uuid.UUID(state.recovery["accounting_message_id"]))
    assert account and float(account.cost_cny) == pytest.approx(0.01)


async def test_attempt_started_is_durable_before_real_transport(
    db_engine: AsyncEngine, db_session: AsyncSession, cfg: AppConfig,
):
    from xhs_agent.core import model_attempts
    from xhs_agent.db.models import ContextObservation
    from xhs_agent.services.model_observations import observer
    from xhs_agent.tools.llm import LLMCall, OpenAICompatibleProvider

    sid = uuid.UUID((await create_session(db_session))["session_id"])
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    cfg.llm.base_url = "https://local.test/v1"

    async def transport(_request):
        async with factory() as separate:
            starts = (await separate.execute(select(ContextObservation).where(
                ContextObservation.session_id == sid,
                ContextObservation.event_type == "model_attempt_started"))).scalars().all()
            assert len(starts) == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "完成"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with model_attempts.observe(observer(factory, cfg, session_id=str(sid))):
            await OpenAICompatibleProvider(cfg.llm, client=client).complete(
                LLMCall(task="context_extract", system="s", user="u"))
    async with factory() as separate:
        events = (await separate.execute(select(ContextObservation).where(
            ContextObservation.session_id == sid))).scalars().all()
        assert sorted(row.event_type for row in events) == ["model_attempt_finished", "model_attempt_started"]
        assert len({row.payload["identity"] for row in events}) == 1


async def test_recovery_second_page_sees_and_replaces_first_candidate(db_session: AsyncSession, cfg: AppConfig):
    sid = await legacy(db_session, 21)
    first = FixedProvider(payload={"operations": [{"op": "add", "kind": "constraint",
        "value": "300字以内", "evidence": "限制内容"}]})
    await recover(db_session, cfg, sid, first)
    stored = await context_state.load(db_session, sid)
    candidate_id = stored.snapshot.proposals[0].proposal_id

    def replace(call, _number):
        assert candidate_id in call.user and "300字以内" in call.user
        return {"operations": [{"op": "replace", "target_fact_id": candidate_id,
                "value": "100字以内", "evidence": "限制内容"}]}

    await recover(db_session, cfg, sid, FixedProvider(payload=replace), expected=stored.state_version,
                  recovery_id=stored.recovery["recovery_id"])
    stored = await context_state.load(db_session, sid)
    assert not stored.snapshot.facts
    assert [p.value for p in stored.snapshot.proposals] == ["100字以内"]
    assert len(stored.snapshot.proposals[0].source_message_ids) == 2
    proposal = stored.snapshot.proposals[0]
    await context_state.perform_action(db_session, sid, action="confirm", target_id=proposal.proposal_id,
        value="", expected_state_version=stored.state_version, action_id=str(uuid.uuid4()))
    stored = await context_state.load(db_session, sid)
    assert context_state.context_view(cfg, stored)["recovery"]["pending_proposal_ids"] == []


async def test_bound_v1_with_v2_head_reaches_real_revise_input_and_replays(
    db_session: AsyncSession, cfg: AppConfig,
):
    from xhs_agent.db.models import Hotspot, Run, RunHotspot

    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    run, hotspot = Run(job_id=str(uuid.uuid4()), status="succeeded"), Hotspot(raw_text="夜跑", clue=clue().to_dict())
    db.add_all([run, hotspot])
    await db.flush()
    original = {"titles": [{"text": "夜跑", "style": "建议"}], "body": "D0 初稿",
                "tags": ["夜跑"], "cover_text": "开始", "first_3s": "慢慢走", "shot_list": [], "compliance_notes": []}
    db.add(RunHotspot(run_id=run.id, hotspot_id=hotspot.id, position=1, status="succeeded", draft=original))
    hotspot.clue = clue().to_dict()
    await db.commit()
    await append_message(db, sid, role="assistant", content="初稿", run_id=str(run.id))
    key = f"draft:{run.id}:1"
    v1 = await artifact_versions.ensure_initial(db, session_id=sid, object_key_value=key,
        object_type="draft", run_id=str(run.id), position=1, content=original)
    claim = await artifact_versions.claim(db, session_id=sid, object_key_value=key, object_type="draft",
        run_id=str(run.id), position=1, request_key_value="head-v2", base_version_id=v1.id,
        expected_head_version_id=v1.id, expected_state_version=0)
    await artifact_versions.commit_success(db, claim_row=claim.row, content={**original, "body": "D1 新版"},
                                           prompt_versions={}, usage={}, expected_state_version=0)
    binding = {"operation": "revise_draft", "object_key": key, "base_version_id": str(v1.id)}
    user = await append_message(db, sid, role="user", content="保持简短", operation_binding=binding)
    state = await db.get(ChatSession, sid)
    state.context_state = {**state.context_state, "facts": [ContextFact(fact_id="f1", kind="constraint",
        value="不要使用感叹号", scope="artifact", scope_id=key, evidence="不要使用感叹号",
        source_ref={"message_id": user["message_id"]}, created_version=1).to_dict()]}
    await db.commit()

    def output(call, _number):
        assert "D0 初稿" in call.user and "D1 新版" not in call.user
        assert "不要使用感叹号" in call.user
        return {"refused": False, "draft": {**original, "body": "D2 从旧版改写"}}

    provider = FixedProvider(payload=output)
    ctx = chat_tools.ToolContext(cfg=cfg, session=db, session_id=sid, caller=StructuredCaller(provider),
        policy=TurnContextPolicy(mode="enforce"), turn_id=str(uuid.uuid4()), checkpoint_scope="call1",
        source_message_id=user["message_id"], operation_binding=await _load_source_binding(
            db, uuid.UUID(user["message_id"]), session_id=sid))
    result = await chat_tools.revise_draft(ctx, {"request": "保持简短"})
    assert result.status == "succeeded", result.error
    assert result.payload["base_version_id"] == str(v1.id)
    replay = await chat_tools.revise_draft(ctx, {"request": "保持简短"})
    assert replay.payload["replayed"] is True and replay.payload["revised"]["body"] == "D2 从旧版改写"
    assert provider.calls == 1


async def test_clue_edit_preserves_trusted_metadata_and_records_actual_fields(db_session: AsyncSession, cfg: AppConfig):
    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    key = "clue:images:" + "b" * 64
    original = clue().to_dict()
    original.update(created_at="2000-01-01T00:00:00+00:00", provider="source", model="original")
    base = await artifact_versions.ensure_initial(db, session_id=sid, object_key_value=key,
        object_type="clue", run_id=None, position=None,
        content={"raw_text": "夜跑", "clue": original, "edited_fields": []})
    edited = {**original, "provider": "forged", "model": "forged", "created_at": "2099-01-01"}
    result = await context_state.edit_clue_version(db, sid, cfg, action_id=str(uuid.uuid4()),
        expected_state_version=0, expected_head_version_id=str(base.id), base_version_id=str(base.id),
        object_key=key, content={"raw_text": "公园夜跑", "clue": edited,
                                  "edited_fields": ["raw_text", "elements", "mechanisms"]})
    content = result["version"]["content"]
    assert content["clue"]["hotspot_raw"] == "公园夜跑"
    assert {name: content["clue"][name] for name in ("provider", "model", "created_at")} == {
        name: original[name] for name in ("provider", "model", "created_at")}
    assert content["edited_fields"] == ["raw_text"]
    assert (await artifact_versions.get_version(db, sid, base.id)).content["clue"] == original


async def test_object_references_require_real_hotspot_and_owned_version(db_session: AsyncSession):
    from xhs_agent.db.models import Hotspot, Run, RunHotspot

    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    other = uuid.UUID((await create_session(db))["session_id"])
    run, hotspot = Run(job_id=str(uuid.uuid4()), status="succeeded"), Hotspot(raw_text="夜跑", clue=clue().to_dict())
    db.add_all([run, hotspot])
    await db.flush()
    db.add(RunHotspot(run_id=run.id, hotspot_id=hotspot.id, position=1, status="succeeded"))
    await db.commit()
    await append_message(db, sid, role="assistant", content="结果", run_id=str(run.id))
    run_id = str(run.id)
    key = f"draft:{run_id}:1"
    other_version = await artifact_versions.ensure_initial(db, session_id=other, object_key_value=key,
        object_type="draft", run_id=str(run.id), position=1, content={"body": "other"})
    invalid = [({"object_key": f"draft:{run.id}:2"}, "hotspot_missing"),
        ({"object_key": key, "version_id": str(other_version.id)}, "artifact_version_mismatch"),
        ({"object_key": "clue:images:" + "c" * 64}, "artifact_version_mismatch"),
        ({"object_key": key, "material_id": str(uuid.uuid4())}, "material_missing")]
    from xhs_agent.core.errors import NotFoundError

    for ref, reason in invalid:
        action = str(uuid.uuid4())
        with pytest.raises(NotFoundError) as info:
            await context_state.perform_action(db, sid, action="select_object", target_id="", value="",
                expected_state_version=0, action_id=action, object_ref=ref)
        assert info.value.detail["reason"] == reason
        assert (await context_state.load(db, sid)).state_version == 0
        row = (await db.execute(select(ChatContextAction).where(ChatContextAction.action_id == uuid.UUID(action)))).scalar_one()
        assert row.status == "failed"
    ref = json.loads(await context_state._validate_object_ref(db, sid, {"object_key": key}))
    assert ref["run_id"] == run_id and ref["hotspot_position"] == 1


@pytest.mark.parametrize("operation", ["extract", "recover"])
@pytest.mark.parametrize("limit", ["fits", "repair_overflow", "initial_overflow"])
async def test_production_context_entry_checks_every_repair(
    db_session: AsyncSession, cfg: AppConfig, monkeypatch: pytest.MonkeyPatch, operation: str, limit: str,
):
    from dataclasses import replace

    from xhs_agent.core import budget
    from xhs_agent.db.models import ContextObservation

    db = db_session
    sid = await legacy(db) if operation == "recover" else uuid.UUID((await create_session(db))["session_id"])
    message = await append_message(db, sid, role="user", content="保持简短") if operation == "extract" else None
    provider = FixedProvider(payload=lambda _call, number: 1 if number == 1 else {"operations": []})
    checks = []
    admissions = []
    real_decide = budget.decide

    def measured(**kwargs):
        if kwargs.get("protected") == ("恢复页用户原文",):
            admissions.append(kwargs)
            result = real_decide(**kwargs)
            return replace(result, allowed=False, reason="required_context_overflow") if limit == "initial_overflow" else result
        checks.append(kwargs)
        result = real_decide(**kwargs)
        if limit == "initial_overflow" or limit == "repair_overflow" and len(checks) == 2:
            return replace(result, allowed=False, reason="required_context_overflow")
        return result

    monkeypatch.setattr(budget, "decide", measured)
    if operation == "recover":
        result = await recover(db, cfg, sid, provider)
        assert result["status"] == ("succeeded" if limit == "fits" else "failed")
    else:
        prep = await _prepare_context(cfg=cfg, session=db, session_id=sid,
            user_message_id=uuid.UUID(message["message_id"]), user_text="保持简短", provider=provider,
            turn_id=str(uuid.uuid4()), ledger={})
        assert prep.status == ("ready" if limit == "fits" else "overflow")
        if limit != "fits":
            assert prep.error_category == "required_context_overflow" and "预算" in prep.error
    assert provider.calls == {"fits": 2, "repair_overflow": 1, "initial_overflow": 0}[limit]
    expected_checks = (0 if operation == "recover" else 1) if limit == "initial_overflow" else 2
    assert len(checks) == expected_checks
    assert len(admissions) == (1 if operation == "recover" else 0)
    assert all(item["output_reserve"] == cfg.context.extract_max_tokens for item in checks)
    events = (await db.execute(select(ContextObservation).where(
        ContextObservation.session_id == sid, ContextObservation.event_type == "context_assembled"))).scalars().all()
    assert len(events) == len(checks) + len(admissions)


async def test_action_terminal_failure_rolls_back_state_and_audit(db_session: AsyncSession, monkeypatch):
    from xhs_agent.db.models import ChatContextEvent

    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    initial = await context_state.load(db, sid)
    action = str(uuid.uuid4())

    async def fail_terminal(*_args, **_kwargs):
        raise RuntimeError("injected terminal failure")

    monkeypatch.setattr(context_state, "_finish_action", fail_terminal)
    with pytest.raises(RuntimeError, match="injected"):
        await context_state.perform_action(db, sid, action="reset_topic", target_id="", value="",
            expected_state_version=0, action_id=action)
    final = await context_state.load(db, sid)
    assert final.state_version == 0 and final.topic_id == initial.topic_id
    assert not (await db.execute(select(ChatContextEvent).where(ChatContextEvent.session_id == sid))).scalars().all()
    row = (await db.execute(select(ChatContextAction).where(ChatContextAction.action_id == uuid.UUID(action)))).scalar_one()
    assert row.status == "failed"


async def test_confirming_unresolved_object_validates_before_activating(db_session: AsyncSession):
    from xhs_agent.core.errors import NotFoundError
    from xhs_agent.schemas import ContextProposal

    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    chat = await db.get(ChatSession, sid)
    proposal = ContextProposal(proposal_id="p1", kind="constraint", value="不要提价格",
        scope="artifact", scope_id="clue:images:" + "d" * 64, reason="scope_unresolved")
    chat.context_state = {**chat.context_state, "proposals": [proposal.to_dict()]}
    await db.commit()
    with pytest.raises(NotFoundError):
        await context_state.perform_action(db, sid, action="confirm", target_id="p1", value="",
            expected_state_version=0, action_id=str(uuid.uuid4()))
    final = await context_state.load(db, sid)
    assert final.state_version == 0 and not final.snapshot.facts
    assert [p.proposal_id for p in final.snapshot.proposals] == ["p1"]


async def test_worker_receives_scoped_constraints_and_persists_transport_events(
    db_engine: AsyncEngine, db_session: AsyncSession, cfg: AppConfig, postgres_dsn: str,
):
    from xhs_agent.config import EnvView
    from xhs_agent.db.models import ContextObservation, Material
    from xhs_agent.services.runs import submit_analysis
    from xhs_agent.tasks.analysis import run_job
    from xhs_agent.tools.llm import OpenAICompatibleProvider

    db = db_session
    cfg._env = EnvView({}, {"DATABASE_URL": postgres_dsn})
    cfg.embedding.enabled = False
    cfg.llm.base_url = "https://worker.test/v1"
    sid = uuid.UUID((await create_session(db))["session_id"])
    chat = await db.get(ChatSession, sid)
    chat.context_state = {**chat.context_state, "facts": [ContextFact(fact_id="f" + str(i), kind="constraint",
        value=value, scope=scope, scope_id=scope_id, evidence=value,
        source_ref={"message_id": "fixture"}, created_version=1).to_dict()
        for i, (value, scope, scope_id) in enumerate([
            ("不要使用感叹号", "session", ""), ("正文保持简短", "topic", chat.topic_id or ""),
            ("不得混入OTHER", "artifact", "draft:other:1")]) ]}
    material = Material(path="D:/fixture/night-run.mp4", type="video", title="夜跑",
                        description="公园夜跑", elements=[], source="sidecar")
    db.add(material)
    await db.commit()
    constraints = await chat_tools._constraints_for(chat_tools.ToolContext(cfg=cfg, session=db,
        session_id=sid, policy=TurnContextPolicy(mode="enforce")))
    submission = await submit_analysis(db, ["夜跑"], clues=[clue().to_dict()], context_constraints=constraints,
        observation_context={"session_id": str(sid), "turn_id": "worker-test", "policy": "enforce"})
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    tasks = []

    async def transport(request):
        body = json.loads(request.content)
        task = "copy_draft" if "constraints_json" in str(body) or "正文保持简短" in str(body) else "material_select"
        tasks.append(task)
        async with factory() as separate:
            starts = (await separate.execute(select(ContextObservation).where(
                ContextObservation.session_id == sid, ContextObservation.event_type == "model_attempt_started"))).scalars().all()
            assert len(starts) == len(tasks)
        if task == "copy_draft":
            prompt_text = str(body["messages"])
            assert "不要使用感叹号" in prompt_text and "正文保持简短" in prompt_text
            assert "不得混入OTHER" not in prompt_text
            payload = {"titles": [{"text": "慢慢夜跑", "style": "建议"}], "body": "慢慢开始",
                "tags": ["夜跑"], "cover_text": "开始", "first_3s": "走出门", "shot_list": [], "compliance_notes": []}
        else:
            payload = {"candidates": [{"material_id": str(material.id), "rank": 1,
                                       "reasons": ["夜跑"], "usage": "开场"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        status = await run_job(submission.job_id, cfg, sync_index=False,
            caller=StructuredCaller(OpenAICompatibleProvider(cfg.llm, client=http)))
    assert status == "succeeded" and tasks == ["material_select", "copy_draft"]
    events = (await db.execute(select(ContextObservation).where(
        ContextObservation.session_id == sid, ContextObservation.event_type.in_(("model_attempt_started", "model_attempt_finished"))))).scalars().all()
    assert len(events) == 4
    assert all(row.payload["endpoint"] == "https://worker.test/v1/chat/completions" for row in events)
    assert all(row.payload["run_id"] == submission.run_id for row in events)


async def test_sync_vision_thread_started_is_visible_before_transport(
    db_engine: AsyncEngine, db_session: AsyncSession, cfg: AppConfig, tmp_path, monkeypatch,
):
    import io
    import urllib.request

    from xhs_agent.config import EnvView, VisionConfig
    from xhs_agent.core import model_attempts
    from xhs_agent.db.models import ContextObservation
    from xhs_agent.services.model_observations import observer
    from xhs_agent.tools.vision import make_describer

    sid = uuid.UUID((await create_session(db_session))["session_id"])
    cfg._env = EnvView({}, {cfg.vision.api_key_env: "fixture-key"})
    cfg.vision._env = cfg._env
    cfg.vision.enabled = True
    monkeypatch.setattr(VisionConfig, "available", lambda _self: True)
    image = tmp_path / "frame.png"
    image.write_bytes(b"fixture")
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()

    async def started_count():
        async with factory() as separate:
            return len((await separate.execute(select(ContextObservation).where(
                ContextObservation.session_id == sid, ContextObservation.event_type == "model_attempt_started"))).scalars().all())

    def transport(*_args, **_kwargs):
        assert asyncio.run_coroutine_threadsafe(started_count(), loop).result(timeout=3) == 1
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"tags":["夜跑"]}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", transport)
    describe = make_describer(cfg)
    with model_attempts.observe(observer(factory, cfg, session_id=str(sid))):
        result = await asyncio.to_thread(describe, [str(image)], "夜跑")
    assert result["tags"] == ["夜跑"]
    rows = (await db_session.execute(select(ContextObservation).where(ContextObservation.session_id == sid))).scalars().all()
    assert sorted(row.event_type for row in rows) == ["model_attempt_finished", "model_attempt_started"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_image_same_bytes_different_path_reuses_within_turn(
    db_session: AsyncSession, cfg: AppConfig, monkeypatch, mode: str, persistent: bool,
):
    sid = uuid.UUID((await create_session(db_session))["session_id"])
    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yN4sAAAAASUVORK5CYII=")
    first = save_attachment(cfg, sid, data, name="first.png", mime="image/png")
    second = save_attachment(cfg, sid, data, name="second.png", mime="image/png")
    fake = AsyncMock(return_value=ImageClueParse(raw_text="夜跑", clue=clue(),
        prompt_tokens=100, completion_tokens=20, cost_cny=0.01, usage_status="actual",
        prompt_versions={"image_hotspot_clue": 3}))
    monkeypatch.setattr(chat_tools, "parse_image_clue", fake)
    ctx = chat_tools.ToolContext(cfg=cfg, session=db_session, session_id=sid,
        policy=TurnContextPolicy(mode=mode, image_cache_enabled=persistent), turn_id=str(uuid.uuid4()))
    first_result = await chat_tools.parse_clue_from_image(ctx, {}, attachments=[first])
    second_result = await chat_tools.parse_clue_from_image(ctx, {}, attachments=[second])
    assert first_result.status == second_result.status == "succeeded"
    assert second_result.payload["usage_status"] == "cached"
    assert second_result.payload["cost_cny"] == 0 and second_result.payload["prompt_tokens"] == 0
    assert fake.call_count == 1


async def test_real_cache_producers_recompute_request_and_entry_rates(db_session: AsyncSession, cfg: AppConfig, monkeypatch):
    import importlib.util

    from xhs_agent.db.models import ContextObservation

    spec = importlib.util.spec_from_file_location("real_cache_metrics", Path(__file__).resolve().parents[2] / "scripts/context_metrics.py")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+yN4sAAAAASUVORK5CYII=")
    attachment = save_attachment(cfg, sid, data, name="fixture.png", mime="image/png")
    fake = AsyncMock(return_value=ImageClueParse(raw_text="夜跑", clue=clue(),
        prompt_versions={"image_hotspot_clue": 3}))
    monkeypatch.setattr(chat_tools, "parse_image_clue", fake)
    ctx = chat_tools.ToolContext(cfg=cfg, session=db, session_id=sid,
        policy=TurnContextPolicy(mode="enforce", image_cache_enabled=True), turn_id=str(uuid.uuid4()))
    since = datetime.now(UTC)
    for _ in range(4):
        assert (await chat_tools.parse_clue_from_image(ctx, {}, attachments=[attachment])).status == "succeeded"
    await db.commit()
    rows = (await db.execute(select(ContextObservation).where(ContextObservation.session_id == sid))).scalars().all()
    observations = [module.Observation(event_id=row.event_id, session_id=str(row.session_id), event_type=row.event_type,
        created_at=row.created_at, turn_id=row.turn_id or "", call_id=row.call_id or "", attempt=row.attempt,
        model=row.model or "", prompt_version=row.prompt_version or "", policy_version=row.policy_version or "",
        payload=row.payload) for row in rows]
    app, _ = module.analyze(observations, since=since, until=datetime.now(UTC))
    assert (app.as_dict()["C"], app.as_dict()["H"], app.as_dict()["M"]) == (4, 3, 1)
    assert app.as_dict()["hit_consumption_rate"] == 1.0
    turn, _ = module.analyze(observations, since=since, until=datetime.now(UTC), cache_layer="turn")
    assert turn.as_dict()["C"] == 3 and turn.as_dict()["H"] == 3
    assert turn.as_dict()["normal_observation_completeness"] == 1.0
    assert turn.as_dict()["entry_reuse_rate"] == 1.0 and fake.call_count == 1


async def test_new_explicit_constraint_is_saved_while_old_proposal_still_blocks(db_session: AsyncSession, cfg: AppConfig):
    from xhs_agent.schemas import ContextProposal

    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    chat = await db.get(ChatSession, sid)
    pending = ContextProposal(proposal_id="p1", kind="goal", value="再看做饭画面", scope="topic",
        scope_id=chat.topic_id or "", reason="ambiguous_reference")
    chat.context_state = {**chat.context_state, "proposals": [pending.to_dict()]}
    await db.commit()
    user = await append_message(db, sid, role="user", content="全对话不要提品牌名称")
    provider = FixedProvider(payload={"operations": [{"op": "add", "kind": "constraint",
        "value": "不要提品牌名称", "scope": "session", "evidence": "全对话不要提品牌名称"}]})
    prep = await _prepare_context(cfg=cfg, session=db, session_id=sid,
        user_message_id=uuid.UUID(user["message_id"]), user_text="全对话不要提品牌名称", provider=provider,
        turn_id=str(uuid.uuid4()), ledger={})
    assert prep.status == "confirmation_required" and provider.calls == 1
    state = await context_state.load(db, sid)
    assert [f.value for f in state.snapshot.facts] == ["不要提品牌名称"]
    assert [p.proposal_id for p in state.snapshot.proposals] == ["p1"]


async def test_other_artifact_pending_proposal_does_not_block_target(db_session: AsyncSession, cfg: AppConfig):
    from xhs_agent.schemas import ContextProposal

    db = db_session
    sid = uuid.UUID((await create_session(db))["session_id"])
    chat = await db.get(ChatSession, sid)
    pending = ContextProposal(proposal_id="other", kind="constraint", value="正文50字以内", scope="artifact",
        scope_id="draft:other:1", reason="conflict")
    chat.context_state = {**chat.context_state, "proposals": [pending.to_dict()]}
    await db.commit()
    user = await append_message(db, sid, role="user", content="改这一版")
    prep = await _prepare_context(cfg=cfg, session=db, session_id=sid,
        user_message_id=uuid.UUID(user["message_id"]), user_text="改这一版", provider=FixedProvider(),
        turn_id=str(uuid.uuid4()), ledger={}, target_object_key="draft:target:1")
    assert prep.status == "ready"
    assert [p.proposal_id for p in (await context_state.load(db, sid)).snapshot.proposals] == ["other"]

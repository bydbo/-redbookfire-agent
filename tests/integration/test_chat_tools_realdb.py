"""对话只读工具的集成用例（S7.7 / S7.7b）：真 Postgres + 假文本模型，全程不联网。

这里证明的是"只读工具在真库上真的能用，且真的是只读"：

- `search_materials`：拆解 → 字面/双通道检索 → 候选摘要与覆盖率；**同一热点只拆一次**（缓存）；
- `preview_element_coverage`：要素 + 覆盖度 + 补拍建议，**不新增 `runs` 行**，**不碰 embedding**
  （用「[embedding].enabled = true 但密钥为空」的配置反证：同样的配置下 `search_materials` 会失败，
  而预览照样成功）；
- 两个工具都不产生 `job_id`、不投递 Celery（用假 enqueue 记录）。
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import (
    Hotspot,
    Material,
    MaterialIdAlias,
    MaterialIdentityBatch,
    Run,
    RunHotspot,
    RunMatch,
)
from xhs_agent.services import artifact_versions
from xhs_agent.services.chat import append_message, create_session
from xhs_agent.services.chat_tools import (
    ToolContext,
    preview_element_coverage,
    revise_draft,
    search_materials,
)
from xhs_agent.services.chat_turns import build_tool_runner
from xhs_agent.services.run_snapshots import evidence_meta, material_snapshot
from xhs_agent.services.runs import submit_analysis
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult, StructuredCaller
from xhs_agent.workflows.chat import run_turn

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

EMBED_KEY_ENV = "SMOKE_ONLY_EMBED_KEY"          # 故意不设：用来证明"没碰 embedding"

# 线索里只有「羽毛球」能被示例素材的字面命中，另外两个要素必然落进缺口。
# 全局关键词只参与召回，命中与覆盖必须分别来自各要素的直接证据或同类型词典。
CLUE_PAYLOAD: dict[str, Any] = {
    "hotspot_raw": "",
    "why_it_works": ["日常器材拍出陌生感"],
    "mechanisms": [{"name": "反差", "explain": "器材特写与运动场景错位"}],
    "elements": [
        {"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9,
         "evidence": "热点原文提到羽毛球"},
        {"type": "scene", "value": "球场", "weight": 0.8, "confidence": 0.8,
         "evidence": "热点原文提到球馆"},
        {"type": "ip", "value": "某顶流明星", "weight": 0.9, "confidence": 0.9,
         "evidence": "热点原文里的明星"},
    ],
    "match_keywords": ["球场"],
    "borrow_angles": ["用同款场地做热场"],
    "risk_notes": [],
}

HOTSPOT = "羽毛球球场随手拍"

# 示例素材：含「羽毛球」（字面通道据此召回）但**不含**「球场」与任何 evidence
MATERIAL_TITLE = "羽毛球新手练习"
MATERIAL_DESCRIPTION = "跟着教练做基础动作"
MATERIAL_TAGS = ["羽毛球", "运动"]


class CountingProvider(llm.BaseProvider):
    """记账的假文本模型：固定回一段线索 JSON，只用来数"拆了几次"。"""

    name = "counting"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        super().__init__(model="counting-1")
        self.payload = payload or CLUE_PAYLOAD
        self.calls = 0
        self.requests: list[llm.LLMCall] = []

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        self.calls += 1
        self.requests.append(call)
        return LLMResult(text=json.dumps(self.payload, ensure_ascii=False),
                         provider=self.name, model=self.model,
                         prompt_tokens=100, completion_tokens=50, cost_cny=0.003)


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def write_config(tmp_path: Path, *, embedding_enabled: bool) -> AppConfig:
    """临时配置：字面召回参数取契约默认值；embedding 按需要开关（密钥一律不设）。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[retrieval]\n"
        "similarity_threshold = 0.2\n"
        "recall_limit = 50\n"
        "hit_threshold = 0.5\n"
        "[match]\n"
        "topk = 5\n"
        "min_score = 0.15\n"
        "[embedding]\n"
        f"enabled = {'true' if embedding_enabled else 'false'}\n"
        f'api_key_env = "{EMBED_KEY_ENV}"\n',
        encoding="utf-8")
    return load_config(str(path))


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    """默认配置：embedding 关（字面通道 + 融合照常跑）。"""
    return write_config(tmp_path, embedding_enabled=False)


@pytest.fixture
def provider() -> CountingProvider:
    return CountingProvider()


@pytest.fixture
def context(db_session: AsyncSession, cfg: AppConfig,
            provider: CountingProvider) -> ToolContext:
    return ToolContext(cfg=cfg, session=db_session,
                       caller=StructuredCaller(provider=provider))


async def seed_material(session: AsyncSession, *, path: str, title: str,
                        description: str, tags: list[str]) -> str:
    """直接插一条素材行（绕过文件扫描），返回 uuid 字符串。"""
    return (await session.execute(text(
        "INSERT INTO materials (path, type, title, description, tags, elements, source, "
        "duration_s, width, height, has_audio, size_bytes, fingerprint) "
        "VALUES (:path, 'video', :title, :description, CAST(:tags AS text[]), "
        "CAST('[]' AS jsonb), 'sidecar', 12, 1080, 1920, false, 0, :fingerprint) "
        "RETURNING id::text"
    ), {"path": path, "title": title, "description": description,
        "tags": list(tags), "fingerprint": f"fp-{path}"})).scalar_one()


async def runs_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(Run))).scalar_one())


REVISED_BODY = "改后的正文"
REVISED_PAYLOAD: dict[str, Any] = {
    "refused": False,
    "draft": {"titles": [{"text": "改后标题", "style": "避坑"}], "cover_text": "封面",
              "first_3s": "开头", "body": REVISED_BODY, "tags": ["#夜跑"],
              "shot_list": ["先拍球鞋"], "compliance_notes": ["别用他人肖像"]},
}
REFUSAL_PAYLOAD: dict[str, Any] = {
    "refused": True, "reason": "不能用他人的姓名或肖像做蹭热度文案",
}


async def seed_finished_run(session: AsyncSession, *, hotspot: str = "夜跑球鞋开箱"
                            ) -> tuple[str, dict[str, Any]]:
    """造一条「已完成」的运行，返回（run_id，原始初稿）。

    与 `test_api_endpoints.py::_finish_run` 同一套口径：走服务层建 run / hotspots /
    run_hotspots，再手工把线索、初稿、候选与统计写回，模拟 worker 干完的活。
    """
    submission = await submit_analysis(session, [hotspot], topk=3)
    run_id = uuid.UUID(str(submission.run_id))
    run_hotspot = (await session.execute(
        select(RunHotspot).where(RunHotspot.run_id == run_id)
        .order_by(RunHotspot.position))).scalars().first()
    assert run_hotspot is not None
    hotspot_row = (await session.execute(
        select(Hotspot).where(Hotspot.id == run_hotspot.hotspot_id))).scalar_one()
    hotspot_row.clue = {
        "hotspot_raw": hotspot,
        "why_it_works": ["夜里跑步的反差"],
        "mechanisms": [{"name": "反差", "explain": "夜里跑步"}],
        "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9, "confidence": 0.9,
                      "evidence": "热点原文"}],
        "match_keywords": ["夜跑"],
        "borrow_angles": ["从夜跑场景切入"],
        "risk_notes": [],
    }
    run_hotspot.raw_text = hotspot
    run_hotspot.clue = dict(hotspot_row.clue)
    run_hotspot.status = "succeeded"
    run_hotspot.coverage = {"ratio": 1.0, "covered": [], "gaps": []}
    run_hotspot.gap_advice = {"items": []}
    run_hotspot.retrieval_meta = {"index_summary": {"materials_count": 1, "digest": "x"}}
    material = Material(path="夜跑球鞋.mp4", type="video", title="夜里的球鞋",
                        description="夜里跑步的球鞋特写", tags=["夜跑"], elements=[],
                        source="sidecar", width=1080, height=1920, size_bytes=2048)
    session.add(material)
    await session.flush()
    draft = {"material_id": str(material.id), "hotspot_key": hotspot,
             "titles": [{"text": "原标题", "style": "直给"}], "body": "原始正文",
             "tags": ["#夜跑"], "cover_text": "", "first_3s": "", "shot_list": [],
             "compliance_notes": []}
    run_hotspot.draft = dict(draft)
    snapshot = material_snapshot(material)
    run_hotspot.evidence_meta = {
        "raw_text": evidence_meta(origin="runtime", source_ref="run:seed/p:1",
                                  completeness="complete", payload=hotspot),
        "clue": evidence_meta(origin="runtime", source_ref="run:seed/p:1",
                              completeness="complete", payload=hotspot_row.clue),
        "gap_advice": evidence_meta(origin="runtime", source_ref="run:seed/p:1",
                                    completeness="complete", payload={"items": []}),
        "retrieval": evidence_meta(origin="runtime", source_ref="run:seed/p:1",
                                   completeness="complete",
                                   payload=run_hotspot.retrieval_meta),
    }
    session.add(RunMatch(run_hotspot_id=run_hotspot.id, material_id=material.id, rank=1,
                         score=Decimal("0.8000"), recall_sources=["literal"],
                         hits=[], missing=[], reasons=["命中主题「夜跑」"],
                         usage="放开头 3 秒", material_snapshot=snapshot,
                         evidence_meta={"candidate": evidence_meta(
                             origin="runtime", source_ref="candidate",
                             completeness="complete", payload=snapshot)}))
    run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
    run.status = "succeeded"
    run.prompt_versions = {"hotspot_clue": 1, "material_select": 1, "copy_draft": 1}
    run.execution_snapshot = {"schema_version": 1, "llm": {"model": "fake"}}
    run.evidence_meta = {"execution": evidence_meta(
        origin="runtime", source_ref="run:seed", completeness="complete",
        payload=run.execution_snapshot)}
    await session.commit()
    return str(run_id), draft


class TestSearchMaterials:
    async def test_returns_candidates_and_reuses_the_clue(
            self, context: ToolContext, db_session: AsyncSession,
            provider: CountingProvider) -> None:
        await seed_material(db_session, path="羽毛球-新手练习.mp4",
                            title=MATERIAL_TITLE, description=MATERIAL_DESCRIPTION,
                            tags=MATERIAL_TAGS)
        await db_session.commit()

        first = await search_materials(context, {"hotspot": HOTSPOT, "topk": 3})
        assert first.status == "succeeded", first.error
        assert first.payload["count"] >= 1
        top = first.payload["candidates"][0]
        assert top["rank"] == 1 and top["score"] > 0
        assert top["title"] == MATERIAL_TITLE
        assert top["tags"][:2] == ["羽毛球", "运动"]
        assert top["usage"]
        assert first.payload["elements"] and first.payload["elements"][0]["label"]
        assert first.prompt_versions == {"hotspot_clue": 2}
        assert first.payload["cost_cny"] > 0
        assert provider.calls == 1

        second = await search_materials(context, {"hotspot": HOTSPOT, "topk": 3})
        assert second.status == "succeeded"
        assert provider.calls == 1                       # 同一轮内同一热点只拆一次
        assert second.payload["cost_cny"] == 0.0         # 缓存命中 → 本次没花钱
        assert await runs_count(db_session) == 0         # 只读：不产生运行记录


class TestPreviewElementCoverage:
    async def test_reports_coverage_and_gap_advice_without_touching_runs(
            self, context: ToolContext, db_session: AsyncSession,
            provider: CountingProvider) -> None:
        await seed_material(db_session, path="羽毛球-新手练习.mp4",
                            title=MATERIAL_TITLE, description=MATERIAL_DESCRIPTION,
                            tags=MATERIAL_TAGS)
        await db_session.commit()

        outcome = await preview_element_coverage(context, {"hotspot": HOTSPOT})

        assert outcome.status == "succeeded", outcome.error
        assert len(outcome.payload["elements"]) == 3
        # 只有「羽毛球」被素材文案命中；「球场」与「人物 IP」都是缺口
        assert 0 < outcome.payload["coverage_ratio"] < 1
        assert [item["type"] for item in outcome.payload["covered"]] == ["topic"]
        assert [item["type"] for item in outcome.payload["gaps"]] == ["scene", "ip"]
        advice = outcome.payload["gap_advice"]
        assert [item["type"] for item in advice] == ["scene", "ip"]      # 顺序与 gaps 一致
        assert "同款" in advice[1]["advice"]                              # IP 的建议来自 gaps.py
        assert "不产出候选素材" in outcome.payload["note"]
        assert outcome.prompt_versions == {"hotspot_clue": 2}
        assert provider.calls == 1
        assert await runs_count(db_session) == 0                # 不新增 runs 行

    async def test_does_not_touch_embedding(self, tmp_path: Path, db_session: AsyncSession,
                                            provider: CountingProvider) -> None:
        """同样的配置下：走双通道的 `search_materials` 会因为缺密钥失败，预览照样成功。"""
        cfg = write_config(tmp_path, embedding_enabled=True)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider=provider))
        await seed_material(db_session, path="羽毛球-新手练习.mp4",
                            title=MATERIAL_TITLE, description=MATERIAL_DESCRIPTION,
                            tags=MATERIAL_TAGS)
        await db_session.commit()

        search = await search_materials(ctx, {"hotspot": HOTSPOT})
        preview = await preview_element_coverage(ctx, {"hotspot": HOTSPOT})

        assert search.status == "failed" and "embedding" in search.error.lower()
        assert preview.status == "succeeded", preview.error
        # 两个工具用的是同一段热点文本：第二次走缓存，所以只拆了一次
        assert provider.calls == 1
        assert await runs_count(db_session) == 0


class TestReviseDraft:
    @pytest.mark.parametrize("mode", ["id_only", "missing_description", "tampered_digest", "malformed_clue"])
    async def test_incomplete_or_tampered_text_refuses_before_model(
            self, db_session: AsyncSession, cfg: AppConfig, mode: str) -> None:
        run_id, _ = await seed_finished_run(db_session)
        if mode == "id_only":
            await db_session.execute(text(
                "UPDATE run_matches SET material_snapshot=jsonb_build_object('id',material_id::text)"))
        elif mode == "missing_description":
            await db_session.execute(text(
                "UPDATE run_matches SET material_snapshot=material_snapshot-'description'"))
        elif mode == "tampered_digest":
            await db_session.execute(text(
                "UPDATE run_matches SET evidence_meta=jsonb_set(evidence_meta,'{candidate,digest}','\"bad\"')"))
        else:
            await db_session.execute(text("UPDATE run_hotspots SET clue='{\"hotspot_raw\":\"x\"}'"))
        await db_session.commit()
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session, caller=StructuredCaller(provider))
        outcome = await revise_draft(ctx, {"run_id": run_id, "request": "改短一点"})
        assert outcome.status == "failed"
        assert outcome.payload["reason"] == "historical_evidence_incomplete"
        assert provider.calls == 0

    async def test_returns_a_new_draft_and_keeps_the_snapshot(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        run_id, original = await seed_finished_run(db_session)
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"run_id": run_id, "request": "改成避坑向"})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["refused"] is False
        assert outcome.payload["revised"]["body"] == REVISED_BODY
        assert outcome.payload["revised"]["material_id"] == original["material_id"]
        assert outcome.prompt_versions == {"copy_revise": 3}
        assert provider.calls == 1
        # 关键：改后文案只在对话里，run 的初稿快照一个字都没动
        db_session.expire_all()
        stored = (await db_session.execute(
            select(RunHotspot.draft).where(RunHotspot.run_id == uuid.UUID(run_id))
        )).scalar_one()
        assert stored["body"] == original["body"]
        assert stored["titles"] == original["titles"]

    async def test_old_material_id_resolves_alias_before_model_call(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        """S6.11b：基础文案持有旧 UUID 时，改稿按别名解析到规范素材，模型看到规范 ID。"""
        run_id, original = await seed_finished_run(db_session)
        canonical = uuid.UUID(original["material_id"])
        old_id = uuid.uuid4()
        batch_id = uuid.uuid4()
        db_session.add(MaterialIdentityBatch(
            id=batch_id, rule_version=2, plan_digest=str(batch_id),
            plan={}, counts={}, status="applied"))
        await db_session.flush()
        db_session.add(MaterialIdAlias(old_id=old_id, canonical_id=canonical,
                                       batch_id=batch_id))
        run_hotspot = (await db_session.execute(
            select(RunHotspot).where(RunHotspot.run_id == uuid.UUID(run_id))
        )).scalar_one()
        run_hotspot.draft = {**run_hotspot.draft, "material_id": str(old_id)}
        # This fixture proves the archived text was captured from that same original UUID.
        match = (await db_session.execute(select(RunMatch).where(
            RunMatch.run_hotspot_id == run_hotspot.id))).scalar_one()
        snapshot = {**match.material_snapshot, "id": str(old_id), "original_material_id": str(old_id)}
        match.material_snapshot = snapshot
        match.evidence_meta = {"candidate": evidence_meta(
            origin="migration_capture", source_ref="e6:verified-original", completeness="complete", payload=snapshot)}
        await db_session.commit()

        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider=provider))
        outcome = await revise_draft(ctx, {"run_id": run_id, "request": "改成避坑向"})

        assert outcome.status == "succeeded", outcome.error
        assert provider.calls == 1
        prompt = provider.requests[0].user
        assert str(canonical) in prompt
        assert str(old_id) in prompt  # original evidence identity remains visible in material JSON
        assert outcome.payload["revised"]["material_id"] == str(canonical)

    async def test_missing_evidence_is_refused_before_any_model_call(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        run_id, _ = await seed_finished_run(db_session)
        await db_session.execute(text(
            "UPDATE run_hotspots SET clue=NULL, evidence_meta='{}'::jsonb "
            "WHERE run_id=:r"), {"r": run_id})
        await db_session.execute(text(
            "UPDATE run_matches SET material_snapshot=NULL, evidence_meta='{}'::jsonb "
            "WHERE run_hotspot_id IN (SELECT id FROM run_hotspots WHERE run_id=:r)"),
            {"r": run_id})
        await db_session.commit()
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider))

        outcome = await revise_draft(ctx, {"run_id": run_id, "request": "改成避坑向"})

        assert outcome.status == "failed"
        assert outcome.payload["reason"] == "historical_evidence_incomplete"
        assert provider.calls == 0

    async def test_base_version_evidence_can_survive_missing_run_snapshot(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        run_id, original = await seed_finished_run(db_session)
        row = (await db_session.execute(text(
            "SELECT material_snapshot::text AS snapshot FROM run_matches LIMIT 1"))).scalar_one()
        snapshot = json.loads(row)
        clue = (await db_session.execute(text(
            "SELECT clue::text FROM run_hotspots WHERE run_id=:r"), {"r": run_id})).scalar_one()
        from xhs_agent.services.run_snapshots import digest
        parsed_clue = json.loads(clue)
        evidence = {"schema_version": 1, "origin": "legacy_run_record",
                    "captured_at": "2026-10-01T00:00:00+00:00",
                    "source_ref": f"run:{run_id}/position:1", "digest": digest({
                        "clue": parsed_clue, "material": snapshot, "base_draft": original}),
                    "canonical_material_id": original["material_id"], "completeness": "complete",
                    "missing_fields": [], "clue": parsed_clue,
                    "material": snapshot, "base_draft": original,
                    "sources": {"clue": evidence_meta(origin="runtime", source_ref="seed:clue",
                        completeness="complete", payload=parsed_clue),
                        "material": evidence_meta(origin="cutover_capture", source_ref="seed:material",
                            completeness="complete", payload=snapshot)}}
        key = f"draft:{run_id}:1"
        sid = uuid.UUID((await create_session(db_session))["session_id"])
        await append_message(db_session, sid, role="assistant", content="draft",
                             run_id=run_id)
        version = await artifact_versions.ensure_initial(
            db_session, session_id=sid, object_key_value=key,
            object_type="draft", run_id=run_id, position=1, content=original,
            evidence_snapshot=evidence)
        # 清理运行快照，只保留基础版本证据
        await db_session.execute(text(
            "UPDATE run_hotspots SET clue=NULL, evidence_meta='{}'::jsonb "
            "WHERE run_id=:r"), {"r": run_id})
        await db_session.execute(text(
            "UPDATE run_matches SET material_snapshot=NULL, evidence_meta='{}'::jsonb "
            "WHERE run_hotspot_id IN (SELECT id FROM run_hotspots WHERE run_id=:r)"),
            {"r": run_id})
        await db_session.commit()
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session, session_id=sid,
                          caller=StructuredCaller(provider))

        outcome = await revise_draft(ctx, {
            "run_id": run_id, "base_version_id": str(version.id),
            "request": "改成避坑向"})

        assert outcome.status == "succeeded", outcome.error
        assert provider.calls == 1

    async def test_refusal_keeps_the_snapshot_and_reports_the_reason(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        run_id, original = await seed_finished_run(db_session)
        provider = CountingProvider(REFUSAL_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"run_id": run_id,
                                           "request": "把明星的名字写进标题"})

        assert outcome.status == "succeeded"          # 拒绝是成功的判断，不是工具故障
        assert outcome.payload["refused"] is True
        assert "肖像" in outcome.payload["reason"]
        assert "revised" not in outcome.payload
        db_session.expire_all()
        stored = (await db_session.execute(
            select(RunHotspot.draft).where(RunHotspot.run_id == uuid.UUID(run_id))
        )).scalar_one()
        assert stored["body"] == original["body"]

    async def test_missing_run_is_reported(self, db_session: AsyncSession,
                                           cfg: AppConfig) -> None:
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider=CountingProvider()))
        outcome = await revise_draft(ctx, {"run_id": str(uuid.uuid4()), "request": "改短点"})
        assert outcome.status == "failed" and "找不到这次运行" in outcome.error

    async def test_without_run_id_falls_back_to_the_session_latest(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        """用户说「刚才那版」时不带 run_id：服务端按会话里最近一次运行兜底（S7.9 冒烟暴露）。"""
        run_id, original = await seed_finished_run(db_session)
        chat = await create_session(db_session)
        chat_id = uuid.UUID(chat["session_id"])
        await append_message(db_session, chat_id, role="assistant", content="跑完了。",
                             status="succeeded", run_id=run_id)
        provider = CountingProvider(REVISED_PAYLOAD)
        ctx = ToolContext(cfg=cfg, session=db_session, session_id=chat_id,
                          caller=StructuredCaller(provider=provider))

        outcome = await revise_draft(ctx, {"request": "改得更口语一点"})

        assert outcome.status == "succeeded", outcome.error
        assert outcome.payload["run_id"] == run_id
        assert outcome.payload["revised"]["body"] == REVISED_BODY
        db_session.expire_all()
        stored = (await db_session.execute(
            select(RunHotspot.draft).where(RunHotspot.run_id == uuid.UUID(run_id))
        )).scalar_one()
        assert stored["body"] == original["body"]

    async def test_without_run_id_in_a_fresh_session_is_reported(
            self, db_session: AsyncSession, cfg: AppConfig) -> None:
        chat = await create_session(db_session)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          session_id=uuid.UUID(chat["session_id"]),
                          caller=StructuredCaller(provider=CountingProvider()))

        outcome = await revise_draft(ctx, {"request": "改得更口语一点"})

        assert outcome.status == "failed" and "还没跑过分析" in outcome.error


class HybridProvider(llm.BaseProvider):
    """一个 provider 顶两个角色：supervisor（带 tools）+ 工具内部的文本调用（按 task 给剧本）。

    `complete_with_tools` 走剧本（每一步给正文与 tool_calls），`complete` 按 task 回固定的
    结构化 JSON——这样一轮对话里的"模型调用"全部可复现，离线也能跑真图真库。
    """

    name = "hybrid"

    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__(model="hybrid-1")
        self.steps = list(steps)
        self.tasks: list[str] = []

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        self.tasks.append(call.task)
        payload = REVISED_PAYLOAD if call.task == "copy_revise" else CLUE_PAYLOAD
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.002)

    async def complete_with_tools(self, call: llm.LLMCall,
                                  on_delta: Any = None) -> LLMResult:
        step = self.steps.pop(0) if self.steps else {"text": "（剧本用完了）"}
        return LLMResult(text=str(step.get("text") or ""), provider=self.name,
                         model=self.model, tool_calls=list(step.get("tool_calls") or []),
                         prompt_tokens=100, completion_tokens=50, cost_cny=0.003)


def tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"call-{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


class TestTurnPromptVersions:
    """S7.7a 的汇总链路在真图上跑一遍：supervisor + 工具内部的 prompt 版本都要落进这一轮。"""

    async def test_read_only_turn_records_both_versions(self, db_session: AsyncSession,
                                                        cfg: AppConfig) -> None:
        await seed_material(db_session, path="羽毛球-新手练习.mp4",
                            title=MATERIAL_TITLE, description=MATERIAL_DESCRIPTION,
                            tags=MATERIAL_TAGS)
        await db_session.commit()
        provider = HybridProvider([
            {"text": "", "tool_calls": [tool_call("search_materials", {"hotspot": HOTSPOT})]},
            {"text": "库里有一条能用的素材。"},
        ])
        runner = build_tool_runner(db_session, cfg=cfg, client=None, session_id=None,
                                   attachments=[], dispatcher=None,
                                   caller=StructuredCaller(provider=provider))

        turn = await run_turn(cfg, provider=provider, history=[], user_text="我库里有没有能用的素材",
                              run_tool=runner)

        assert turn.status == "succeeded", turn.error
        assert [item["tool"] for item in turn.tool_calls] == ["search_materials"]
        assert turn.tool_calls[0]["status"] == "succeeded"
        # supervisor 的版本（v2）+ 工具内部拆解的版本，都记进这一轮
        assert turn.prompt_versions == {"chat_supervisor": 6, "hotspot_clue": 2}
        assert provider.tasks == ["hotspot_clue"]
        assert turn.run_id == ""                      # 只读工具不产生 run
        assert turn.cost_cny > 0

    async def test_revise_turn_records_the_revise_version(self, db_session: AsyncSession,
                                                          cfg: AppConfig) -> None:
        run_id, _original = await seed_finished_run(db_session)
        provider = HybridProvider([
            {"text": "", "tool_calls": [tool_call("revise_draft",
                                                  {"run_id": run_id, "request": "改成避坑向"})]},
            {"text": "改好了，这版更偏避坑。"},
        ])
        runner = build_tool_runner(db_session, cfg=cfg, client=None, session_id=None,
                                   attachments=[], dispatcher=None,
                                   caller=StructuredCaller(provider=provider))

        turn = await run_turn(cfg, provider=provider, history=[], user_text="把刚才那版改得更避坑",
                              run_tool=runner)

        assert turn.status == "succeeded", turn.error
        assert turn.tool_calls[0]["tool"] == "revise_draft"
        assert turn.prompt_versions == {"chat_supervisor": 6, "copy_revise": 3}
        assert provider.tasks == ["copy_revise"]

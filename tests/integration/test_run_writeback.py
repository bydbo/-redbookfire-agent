"""分析结果写回的集成用例（S3.4a）：真容器 + 假模型 + 假 embedder，不联网。

覆盖：逐热点写回、`runs` 统计与 prompt 版本、写回幂等、线索复用跳过拆解、
`runs.topk` 真的影响检索、单热点失败不阻断整批、`reasons` 非空由数据库 CHECK 兜底、
产物目录按 `runs/<run_id>/` 落盘且不再有 `state.json`（ADR 0011）。
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from langgraph.types import RetryPolicy
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM, Hotspot, Run, RunHotspot, RunMatch
from xhs_agent.services.runs import planned_hotspots, submit_analysis
from xhs_agent.tools import llm
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.llm import LLMResult, StructuredCaller
from xhs_agent.workflows import NODE_STEPS, run_analysis
from xhs_agent.workflows import analysis as workflow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)   # 与素材向量一致 → 余弦距离 0
NOW = 1_760_000_000.0
FAST_RETRY = RetryPolicy(max_attempts=3, initial_interval=0.01, backoff_factor=1.0,
                         max_interval=0.05, jitter=False, retry_on=workflow.retry_on_error)
FAIL_MARKER = "失败热点"
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

CLUE_PAYLOAD = {
    "hotspot_raw": "模型改写版（应被输入覆盖）",
    "why_it_works": ["反差：明星身份 vs 业余球场"],
    "mechanisms": [{"name": "反差", "explain": "身份反差"}],
    "elements": [{"type": "topic", "value": "羽毛球", "weight": 0.9, "confidence": 0.9,
                  "evidence": "热点原文片段"}],
    "match_keywords": ["羽毛球"],
    "borrow_angles": ["同款球场热场"],
}
DRAFT_PAYLOAD = {"titles": [{"text": "球场热身也能出片", "style": "直给"}], "body": "正文……",
                 "tags": ["羽毛球"], "cover_text": "封面", "first_3s": "开头",
                 "shot_list": ["先拍球场"], "compliance_notes": ["别用明星肖像"]}


class FakeEmbedder:
    """固定返回 E0：查询向量与素材向量一致，向量通道必然命中。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls += 1
        return EmbeddingResult(vectors=[list(E0) for _ in texts], model=MODEL,
                               prompt_tokens=12, attempts=1)


class FakeProvider(llm.BaseProvider):
    """material_select 要与候选一一对应，所以从 prompt 里回读候选 uuid 再逐条解释。"""

    name = "fake"

    def __init__(self, *, fail_on_marker: str | None = None) -> None:
        super().__init__(model="fake-1")
        self.tasks: list[str] = []
        self.fail_on_marker = fail_on_marker

    async def complete(self, call):
        self.tasks.append(call.task)
        if self.fail_on_marker and self.fail_on_marker in call.user:
            return LLMResult(text="", provider=self.name, model=self.model, error="假上游失败")
        if call.task == "material_select":
            ids = list(dict.fromkeys(UUID_RE.findall(call.user)))
            payload = {"candidates": [
                {"material_id": material_id, "rank": index,
                 "reasons": [f"模型理由：命中主题「羽毛球」（{index}）"],
                 "usage": "模型用法：放开头 3 秒"}
                for index, material_id in enumerate(ids, start=1)
            ]}
        else:
            payload = {"hotspot_clue": CLUE_PAYLOAD, "copy_draft": DRAFT_PAYLOAD}[call.task]
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), provider=self.name,
                         model=self.model, prompt_tokens=100, completion_tokens=50,
                         cost_cny=0.0001)


def write_config(tmp_path: Path) -> AppConfig:
    """临时配置：句柄指向 tmp（不写仓库），其余走代码默认（= 契约默认）。"""
    path = tmp_path / "config.toml"
    path.write_text(
        "[paths]\n"
        f'materials_dir = "{(tmp_path / "materials").as_posix()}"\n'
        f'runs_dir = "{(tmp_path / "runs").as_posix()}"\n'
        "[embedding]\nenabled = true\n"
        f'model = "{MODEL}"\n',
        encoding="utf-8",
    )
    return load_config(str(path))


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed_material(session: AsyncSession, *, path: str, title: str,
                        description: str) -> str:
    """直接插一行素材（带与假 embedder 一致的向量），返回 uuid 字符串。"""
    literal = "[" + ",".join(repr(float(value)) for value in E0) + "]"
    return (await session.execute(text(
        "INSERT INTO materials (path, type, title, description, tags, elements, source, "
        "duration_s, width, height, has_audio, size_bytes, fingerprint, embedding, embedding_model) "
        "VALUES (:path, 'video', :title, :description, CAST(:tags AS text[]), '[]'::jsonb, "
        "'sidecar', 15, 1080, 1920, false, 1024, :fingerprint, CAST(:embedding AS vector), :model) "
        "RETURNING id::text"
    ), {"path": path, "title": title, "description": description, "tags": ["羽毛球", "运动"],
        "fingerprint": f"fp-{path}", "embedding": literal, "model": MODEL})).scalar_one()


async def seed_library(session: AsyncSession, count: int = 3) -> list[str]:
    titles = [("羽毛球球场热身", "户外球场挥拍"), ("羽毛球反手教学", "慢动作分解"),
              ("羽毛球夜间灯光场", "城市夜拍")]
    return [await seed_material(session, path=f"D:/pack/court-{index}.mp4", title=title,
                                description=description)
            for index, (title, description) in enumerate(titles[:count])]


async def start_run(session: AsyncSession, hotspots: list[str], *,
                    topk: int = 5) -> str:
    """按 S3.3 的提交路径建好 runs / run_hotspots，再按 position 读出待分析原文。"""
    submission = await submit_analysis(session, hotspots, topk=topk)
    assert [raw for _, raw in await planned_hotspots(session, submission.run_id)] == hotspots
    return submission.run_id


async def run_for(session: AsyncSession, run_id: str, cfg: AppConfig, *,
                  provider: FakeProvider, embedder: FakeEmbedder | None = None,
                  retry_policy: RetryPolicy | None = None):
    raws = [raw for _, raw in await planned_hotspots(session, run_id)]
    return await run_analysis(raws, cfg=cfg, session=session, run_id=run_id,
                              caller=StructuredCaller(provider=provider),
                              embedder=embedder or FakeEmbedder(), now=NOW,
                              retry_policy=retry_policy)


class TestWriteBack:
    async def test_persists_the_whole_result(self, db_session: AsyncSession,
                                             tmp_path: Path) -> None:
        material_ids = await seed_library(db_session)
        cfg = write_config(tmp_path)
        run_id = await start_run(db_session, ["某明星打球场被拍，反差感拉满"])
        provider = FakeProvider()

        result = await run_for(db_session, run_id, cfg, provider=provider)

        assert result.status == "succeeded"
        assert provider.tasks == ["hotspot_clue", "material_select", "copy_draft"]

        run = await db_session.get(Run, uuid.UUID(run_id))
        assert run.status == "succeeded"
        assert run.started_at is not None and run.finished_at is not None
        assert run.llm_calls == 3
        assert run.prompt_tokens == 300 and run.completion_tokens == 150
        assert float(run.cost_cny) == pytest.approx(0.0003)
        assert run.latency_ms >= 0
        assert run.prompt_versions == {"hotspot_clue": 1, "material_select": 1, "copy_draft": 1}
        assert run.error is None

        run_hotspot = await _single_run_hotspot(db_session, run_id)
        assert run_hotspot.status == "succeeded"
        assert run_hotspot.coverage["ratio"] == pytest.approx(1.0)
        assert run_hotspot.draft["body"] == "正文……"
        assert run_hotspot.error is None

        hotspot = await db_session.get(Hotspot, run_hotspot.hotspot_id)
        assert hotspot.clue["hotspot_raw"] == "某明星打球场被拍，反差感拉满"

        matches = await _matches(db_session, run_hotspot.id)
        assert len(matches) == len(material_ids)
        assert [row.rank for row in matches] == [1, 2, 3]
        assert {str(row.material_id) for row in matches} == set(material_ids)
        for row in matches:
            assert row.reasons, "契约不允许无理由候选"
            assert row.recall_sources == ["literal", "vector"]
            assert row.usage.startswith("模型用法")

    async def test_rerun_is_idempotent(self, db_session: AsyncSession, tmp_path: Path) -> None:
        await seed_library(db_session)
        cfg = write_config(tmp_path)
        run_id = await start_run(db_session, ["某明星打球场被拍"])

        await run_for(db_session, run_id, cfg, provider=FakeProvider())
        before = await _match_count(db_session, run_id)
        await run_for(db_session, run_id, cfg, provider=FakeProvider())

        assert before == 3
        assert await _match_count(db_session, run_id) == before   # 先删后插 → 不重复
        assert await _run_hotspot_count(db_session, run_id) == 1

    async def test_second_submission_reuses_clue_and_skips_extraction(
            self, db_session: AsyncSession, tmp_path: Path) -> None:
        await seed_library(db_session)
        cfg = write_config(tmp_path)
        raw = "某明星打球场被拍"

        first = await start_run(db_session, [raw])
        await run_for(db_session, first, cfg, provider=FakeProvider())

        second = await start_run(db_session, [raw])   # 同一句热点 → 复用 hotspots 行
        provider = FakeProvider()
        result = await run_for(db_session, second, cfg, provider=provider)

        assert result.status == "succeeded"
        assert provider.tasks == ["material_select", "copy_draft"]   # 拆解被跳过
        assert "hotspot_clue" not in result.prompt_versions
        assert result.prompt_versions == {"material_select": 1, "copy_draft": 1}
        assert await _match_count(db_session, second) == 3

    async def test_topk_from_run_row_truncates_matches(self, db_session: AsyncSession,
                                                       tmp_path: Path) -> None:
        await seed_library(db_session)
        cfg = write_config(tmp_path)
        run_id = await start_run(db_session, ["某明星打球场被拍"], topk=2)

        await run_for(db_session, run_id, cfg, provider=FakeProvider())

        # cfg.match.topk 是默认的 5，但 runs.topk=2 必须真的生效
        assert cfg.match.topk == 5
        assert await _match_count(db_session, run_id) == 2

    async def test_failed_hotspot_is_persisted_and_batch_continues(
            self, db_session: AsyncSession, tmp_path: Path) -> None:
        await seed_library(db_session)
        cfg = write_config(tmp_path)
        run_id = await start_run(db_session, [FAIL_MARKER, "正常热点"])

        result = await run_for(db_session, run_id, cfg, provider=FakeProvider(
            fail_on_marker=FAIL_MARKER), retry_policy=FAST_RETRY)

        assert result.status == "failed" and len(result.errors) == 1
        rows = (await db_session.execute(
            select(RunHotspot).where(RunHotspot.run_id == uuid.UUID(run_id))
            .order_by(RunHotspot.position))).scalars().all()
        assert [row.status for row in rows] == ["failed", "succeeded"]
        assert rows[0].error and FAIL_MARKER not in rows[0].error
        assert rows[0].coverage == {} and rows[0].draft is None
        assert rows[1].draft["body"] == "正文……"
        assert await _match_count(db_session, run_id) == 3   # 失败热点不产候选

        run = await db_session.get(Run, uuid.UUID(run_id))
        assert run.status == "failed" and run.error

    async def test_empty_reasons_violate_the_database_constraint(
            self, db_session: AsyncSession, tmp_path: Path) -> None:
        material_id = (await seed_library(db_session, count=1))[0]
        cfg = write_config(tmp_path)
        run_id = await start_run(db_session, ["某明星打球场被拍"])
        await run_for(db_session, run_id, cfg, provider=FakeProvider())
        run_hotspot = await _single_run_hotspot(db_session, run_id)
        await db_session.commit()

        with pytest.raises(IntegrityError):
            await db_session.execute(text(
                "INSERT INTO run_matches (run_hotspot_id, material_id, rank, score, reasons) "
                "VALUES (CAST(:rh AS uuid), CAST(:m AS uuid), 9, 0.5, '[]'::jsonb)"
            ), {"rh": str(run_hotspot.id), "m": material_id})
            await db_session.commit()
        await db_session.rollback()

    async def test_artifacts_land_under_run_id_without_state_json(
            self, db_session: AsyncSession, tmp_path: Path) -> None:
        await seed_library(db_session)
        cfg = write_config(tmp_path)
        run_id = await start_run(db_session, ["某明星打球场被拍"])

        result = await run_for(db_session, run_id, cfg, provider=FakeProvider())

        run_dir = Path(cfg.runs_dir()) / run_id
        assert run_dir.is_dir()
        assert Path(result.report_paths["markdown"]).parent == run_dir
        names = sorted(item.name for item in run_dir.iterdir())
        assert names == ["report.html", "report.md", "trace.jsonl"]   # ADR 0011：无 state.json
        events = [json.loads(line) for line
                  in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        steps = [event["payload"]["name"] for event in events if event["event"] == "step_started"]
        assert steps == list(NODE_STEPS)


async def _single_run_hotspot(session: AsyncSession, run_id: str) -> RunHotspot:
    return (await session.execute(
        select(RunHotspot).where(RunHotspot.run_id == uuid.UUID(run_id)))).scalar_one()


async def _matches(session: AsyncSession, run_hotspot_id) -> list[RunMatch]:
    return list((await session.execute(
        select(RunMatch).where(RunMatch.run_hotspot_id == run_hotspot_id)
        .order_by(RunMatch.rank))).scalars().all())


async def _match_count(session: AsyncSession, run_id: str) -> int:
    return int((await session.execute(
        select(func.count()).select_from(RunMatch)
        .join(RunHotspot, RunHotspot.id == RunMatch.run_hotspot_id)
        .where(RunHotspot.run_id == uuid.UUID(run_id)))).scalar_one())


async def _run_hotspot_count(session: AsyncSession, run_id: str) -> int:
    return int((await session.execute(
        select(func.count()).select_from(RunHotspot)
        .where(RunHotspot.run_id == uuid.UUID(run_id)))).scalar_one())

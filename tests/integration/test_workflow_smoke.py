"""五节点工作流的集成冒烟（S3.1）：真容器 + 真检索 + 假模型，不联网。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import EMBEDDING_DIM
from xhs_agent.tools import llm
from xhs_agent.tools.embedding import EmbeddingResult
from xhs_agent.tools.llm import LLMResult, StructuredCaller
from xhs_agent.workflows import NODE_STEPS, run_analysis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

MODEL = "text-embedding-v3"
E0 = [1.0] + [0.0] * (EMBEDDING_DIM - 1)   # 与假 embedder 的查询向量一致 → 距离 0

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
    """material_select 的条目要与真实素材 uuid 对上，所以 id 由构造参数传入。"""

    name = "fake"

    def __init__(self, material_id: str) -> None:
        super().__init__(model="fake-1")
        self.material_id = material_id
        self.tasks: list[str] = []

    async def complete(self, call):
        self.tasks.append(call.task)
        payloads = {
            "hotspot_clue": CLUE_PAYLOAD,
            "material_select": {"candidates": [
                {"material_id": self.material_id, "rank": 1,
                 "reasons": ["模型理由：命中主题「羽毛球」"], "usage": "模型用法：放开头 3 秒"},
            ]},
            "copy_draft": DRAFT_PAYLOAD,
        }
        return LLMResult(text=json.dumps(payloads[call.task], ensure_ascii=False),
                         provider=self.name, model=self.model,
                         prompt_tokens=100, completion_tokens=50, cost_cny=0.0001)


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


async def seed(session: AsyncSession, *, path: str, title: str, description: str,
               tags: list[str], embedding: list[float]) -> str:
    literal = "[" + ",".join(repr(float(value)) for value in embedding) + "]"
    return (await session.execute(text(
        "INSERT INTO materials (path, type, title, description, tags, elements, source, "
        "duration_s, width, height, has_audio, size_bytes, fingerprint, embedding, embedding_model) "
        "VALUES (:path, 'video', :title, :description, CAST(:tags AS text[]), '[]'::jsonb, "
        "'sidecar', 15, 1080, 1920, false, 1024, :fingerprint, CAST(:embedding AS vector), :model) "
        "RETURNING id::text"
    ), {"path": path, "title": title, "description": description, "tags": list(tags),
        "fingerprint": f"fp-{path}", "embedding": literal, "model": MODEL})).scalar_one()


class TestWorkflowSmoke:
    async def test_runs_five_nodes_against_real_retrieval(self, db_session: AsyncSession,
                                                          tmp_path: Path) -> None:
        # 通道 A 的文本是 title || description（不含 tags），所以关键词要写进标题/描述
        material_id = await seed(db_session, path="D:/pack/球场热身.mp4", title="羽毛球球场热身",
                                 description="户外球场挥拍", tags=["羽毛球", "运动"],
                                 embedding=E0)
        cfg = write_config(tmp_path)
        provider = FakeProvider(material_id)

        result = await run_analysis(["某明星打球场被拍，反差感拉满"], cfg=cfg,
                                    session=db_session, embedder=FakeEmbedder(),
                                    caller=StructuredCaller(provider=provider))

        assert result.status == "succeeded"
        assert result.prompt_versions == {"hotspot_clue": 1, "material_select": 1,
                                          "copy_draft": 1}
        assert provider.tasks == ["hotspot_clue", "material_select", "copy_draft"]

        entry = result.hotspots[0]
        assert entry["clue"]["hotspot_raw"] == "某明星打球场被拍，反差感拉满"
        candidate = entry["candidates"][0]
        assert candidate["material"]["title"] == "羽毛球球场热身"
        assert candidate["recall_sources"] == ["literal", "vector"]   # 两条通道都召回
        assert candidate["reasons"] == ["模型理由：命中主题「羽毛球」"]  # 模型解释覆盖规则解释
        assert entry["draft"]["body"] == "正文……"

        assert Path(result.report_paths["html"]).is_file()
        markdown = Path(result.report_paths["markdown"]).read_text(encoding="utf-8")
        assert "某明星打球场被拍" in markdown
        assert "向量覆盖率 1/1（100%）" in markdown

        # ADR 0011：状态以数据库为准，产物目录只有报告与 trace.jsonl（不再有 state.json）
        run_dir = Path(result.report_paths["markdown"]).parent
        assert Path(cfg.runs_dir()) in run_dir.parents
        assert not (run_dir / "state.json").exists()
        events = [json.loads(line) for line
                  in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        steps = [event["payload"]["name"] for event in events if event["event"] == "step_started"]
        assert steps == list(NODE_STEPS)

    async def test_empty_library_still_produces_report(self, db_session: AsyncSession,
                                                      tmp_path: Path) -> None:
        cfg = write_config(tmp_path)
        provider = FakeProvider("m_none")
        result = await run_analysis(["冷门到没有任何素材的热点"], cfg=cfg, session=db_session,
                                    embedder=FakeEmbedder(),
                                    caller=StructuredCaller(provider=provider))
        assert result.status == "succeeded"
        assert result.prompt_versions == {"hotspot_clue": 1}    # 没候选就不调解释与撰稿
        assert provider.tasks == ["hotspot_clue"]
        entry = result.hotspots[0]
        assert entry["candidates"] == [] and entry["draft"] is None
        assert entry["gap_advice"][0]["type"] == "topic"
        markdown = Path(result.report_paths["markdown"]).read_text(encoding="utf-8")
        assert "缺少 [主题] 羽毛球 → 补拍主题相关素材" in markdown

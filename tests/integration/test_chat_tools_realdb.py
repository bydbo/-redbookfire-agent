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
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.config import AppConfig, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Run
from xhs_agent.services.chat_tools import (
    ToolContext,
    preview_element_coverage,
    search_materials,
)
from xhs_agent.tools import llm
from xhs_agent.tools.llm import LLMResult, StructuredCaller

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

EMBED_KEY_ENV = "SMOKE_ONLY_EMBED_KEY"          # 故意不设：用来证明"没碰 embedding"

# 线索里只有「羽毛球」能被示例素材的字面命中，另外两个要素必然落进缺口。
#
# **夹具为什么长这样**（踩过一次，别再改回去）：`tools/matching.py` 有一条既有行为
# ——只要任一 `match_keywords` 或 `element.evidence` 是素材文案的**子串**，就会给**每一个**
# 线索要素都抬到 0.9 相似度（见 `tests/unit/test_matching.py::test_keyword_hit_is_not_type_aware`）。
# 所以 `match_keywords`（这里是「球场」）与所有 `evidence` 都必须**不出现在素材文案里**，
# 否则覆盖率恒为 1.0、缺口恒为空，这条用例就白测了。
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

    def __init__(self) -> None:
        super().__init__(model="counting-1")
        self.calls = 0

    async def complete(self, call: llm.LLMCall) -> LLMResult:
        self.calls += 1
        return LLMResult(text=json.dumps(CLUE_PAYLOAD, ensure_ascii=False),
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


class TestSearchMaterials:
    async def test_returns_candidates_and_reuses_the_clue(
            self, context: ToolContext, db_session: AsyncSession,
            provider: CountingProvider) -> None:
        await seed_material(db_session, path="D:/materials/羽毛球-新手练习.mp4",
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
        assert first.prompt_versions == {"hotspot_clue": 1}
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
        await seed_material(db_session, path="D:/materials/羽毛球-新手练习.mp4",
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
        assert outcome.prompt_versions == {"hotspot_clue": 1}
        assert provider.calls == 1
        assert await runs_count(db_session) == 0                # 不新增 runs 行

    async def test_does_not_touch_embedding(self, tmp_path: Path, db_session: AsyncSession,
                                            provider: CountingProvider) -> None:
        """同样的配置下：走双通道的 `search_materials` 会因为缺密钥失败，预览照样成功。"""
        cfg = write_config(tmp_path, embedding_enabled=True)
        ctx = ToolContext(cfg=cfg, session=db_session,
                          caller=StructuredCaller(provider=provider))
        await seed_material(db_session, path="D:/materials/羽毛球-新手练习.mp4",
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

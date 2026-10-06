"""S6.7 预置线索通道的集成用例（真 Postgres 容器）：写入、覆盖与"不传时不变"。

接口层校验已由 `tests/unit/test_clues_request.py` 覆盖；这里只看数据库里的 `hotspots.clue` 事实，
以及"复用已有快照"这条既有语义没有被新通道破坏。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from xhs_agent.db import Base
from xhs_agent.db.models import Hotspot
from xhs_agent.services.runs import submit_analysis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CLUE: dict = {
    "why_it_works": ["自律夜跑与路边宵夜并置，形成反差"],
    "mechanisms": [{"name": "反差", "explain": "自律与放纵同框"}],
    "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9, "confidence": 0.9,
                  "evidence": "背对镜头慢跑"}],
    "match_keywords": ["夜跑", "夜景"],
    "borrow_angles": ["用同款夜景跑道做热场"],
}


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def clue_of(session: AsyncSession, raw: str) -> dict:
    row = (await session.execute(
        select(Hotspot).where(Hotspot.raw_text == raw))).scalar_one()
    return dict(row.clue or {})


class TestPresetClues:
    async def test_writes_clue_and_overwrites_existing_snapshot(self, db_session):
        # 第一次：不带 clues → 建行且线索为空（等 worker 拆解）
        await submit_analysis(db_session, ["某热点"])
        assert await clue_of(db_session, "某热点") == {}

        # 第二次：带 clues → 写入，并把 hotspot_raw 覆盖成当前原文
        await submit_analysis(db_session, ["某热点"],
                              clues=[{**CLUE, "hotspot_raw": "模型给的旧原文"}])
        clue = await clue_of(db_session, "某热点")
        assert clue["hotspot_raw"] == "某热点"
        assert clue["elements"][0]["value"] == "夜跑"

        # 第三次：同一原文带不同线索 → 覆盖（用户编辑优先）
        await submit_analysis(db_session, ["某热点"],
                              clues=[{**CLUE, "match_keywords": ["夜景", "跑道"]}])
        latest = await clue_of(db_session, "某热点")
        assert latest["match_keywords"] == ["夜景", "跑道"]

    async def test_duplicate_raw_text_keeps_the_last_clue(self, db_session):
        await submit_analysis(
            db_session, ["同一条", "同一条"],
            clues=[{**CLUE, "match_keywords": ["第一条"]},
                   {**CLUE, "match_keywords": ["第二条"]}])
        clue = await clue_of(db_session, "同一条")
        assert clue["match_keywords"] == ["第二条"]

    async def test_null_clue_keeps_the_existing_snapshot(self, db_session):
        await submit_analysis(db_session, ["某热点"], clues=[CLUE])
        await submit_analysis(db_session, ["某热点", "另一个热点"], clues=[None, CLUE])
        clue = await clue_of(db_session, "某热点")
        assert clue["match_keywords"] == ["夜跑", "夜景"]

    async def test_without_clues_does_not_clear_existing_snapshot(self, db_session):
        await submit_analysis(db_session, ["某热点"], clues=[CLUE])
        await submit_analysis(db_session, ["某热点"])          # 不带 clues 的提交
        clue = await clue_of(db_session, "某热点")
        assert clue["elements"][0]["value"] == "夜跑"

    async def test_length_mismatch_is_rejected_by_the_service(self, db_session):
        with pytest.raises(ValueError, match="等长"):
            await submit_analysis(db_session, ["热点一", "热点二"], clues=[CLUE])

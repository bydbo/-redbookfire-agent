"""E8 历史快照回归：提交冻结、素材变化不改历史、报告不重算、预览归属。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from test_run_writeback import (
    FakeProvider,
    run_for,
    seed_library,
    start_run,
    write_config,
)

from xhs_agent.api.routers.result import get_run_candidate_keyframe
from xhs_agent.core.errors import ConflictError, NotFoundError
from xhs_agent.db import Base
from xhs_agent.services.run_snapshots import canonical
from xhs_agent.services.runs import (
    RunRecorder,
    load_report_model,
    load_run,
    submit_analysis,
)
from xhs_agent.tools.report import render_markdown
from xhs_agent.workflows import analysis as analysis_workflow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def schema(db_engine: AsyncEngine, reset_database: None) -> None:
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _run(db_session: AsyncSession, tmp_path: Path):
    await seed_library(db_session)
    cfg = write_config(tmp_path)
    run_id = await start_run(db_session, ["某明星打球场被拍，反差感拉满"])
    await run_for(db_session, run_id, cfg, provider=FakeProvider())
    return cfg, run_id


async def test_material_edit_and_delete_do_not_change_history(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    cfg, run_id = await _run(db_session, tmp_path)
    before = await load_run(db_session, uuid.UUID(run_id), cfg)
    candidate = before["hotspots"][0]["candidates"][0]
    saved = dict(candidate["material"])
    material_id = candidate["material_id"]
    report_before = render_markdown(
        await load_report_model(db_session, cfg, uuid.UUID(run_id)))

    await db_session.execute(text(
        "UPDATE materials SET title='新标题', tags=ARRAY['新标签'] WHERE id=:id"),
        {"id": material_id})
    old_path = saved["path"]
    await db_session.execute(text("DELETE FROM materials WHERE id=:id"), {"id": material_id})
    new_id = (await db_session.execute(text(
        "INSERT INTO materials (path,title) VALUES (:p,'同路径新素材') RETURNING id::text"),
        {"p": old_path})).scalar_one()
    await db_session.commit()

    after = await load_run(db_session, uuid.UUID(run_id), cfg)
    candidate_after = after["hotspots"][0]["candidates"][0]
    assert candidate_after["material"] == saved
    assert candidate_after["material"]["title"] != "新标题"
    assert candidate_after["material_id"] == material_id
    assert candidate_after["material_id"] != new_id
    assert candidate_after["current_material"]["exists"] is False
    assert len(after["hotspots"][0]["candidates"]) == len(before["hotspots"][0]["candidates"])
    report_after = render_markdown(
        await load_report_model(db_session, cfg, uuid.UUID(run_id)))
    assert saved["title"] in report_after
    assert "新标题" not in report_after
    assert report_after == report_before


async def test_report_uses_saved_config_and_gap_advice(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, run_id = await _run(db_session, tmp_path)
    model = await load_report_model(db_session, cfg, uuid.UUID(run_id))
    assert model["config"]["llm"]["model"] == cfg.llm.model
    stored_advice = model["hotspots"][0]["gap_advice"]
    changed = cfg.model_copy(deep=True)
    changed.llm.model = "a-different-model"

    import xhs_agent.tools.gaps as gap_tool
    monkeypatch.setattr(gap_tool, "gap_advice", lambda _coverage: [{"mutation": True}])
    again = await load_report_model(db_session, changed, uuid.UUID(run_id))

    assert again["config"]["llm"]["model"] == cfg.llm.model
    assert again["hotspots"][0]["gap_advice"] == stored_advice


async def test_submit_freezes_clue_per_run(db_session: AsyncSession) -> None:
    clue_a = {"hotspot_raw": "同一个热点", "why_it_works": ["A"],
              "mechanisms": [], "elements": [], "match_keywords": ["A"]}
    clue_b = {"hotspot_raw": "同一个热点", "why_it_works": ["B"],
              "mechanisms": [], "elements": [], "match_keywords": ["B"]}
    first = await submit_analysis(db_session, ["同一个热点"], clues=[clue_a])
    second = await submit_analysis(db_session, ["同一个热点"], clues=[clue_b])

    async def stored(run_id: str):
        return (await db_session.execute(text(
            "SELECT clue::text FROM run_hotspots WHERE run_id=:r"), {"r": run_id})).scalar_one()

    assert json.loads(await stored(first.run_id))["match_keywords"] == ["A"]
    assert json.loads(await stored(second.run_id))["match_keywords"] == ["B"]
    # 提交后再改共享缓存，不影响第二条运行
    await db_session.execute(text(
        "UPDATE hotspots SET clue=:c WHERE raw_text='同一个热点'"), {"c": canonical(clue_a)})
    await db_session.commit()
    assert json.loads(await stored(second.run_id))["match_keywords"] == ["B"]


async def test_new_run_copies_cached_clue_at_submit(db_session: AsyncSession) -> None:
    clue = {"hotspot_raw": "缓存热点", "why_it_works": ["缓存"],
            "mechanisms": [], "elements": [], "match_keywords": ["缓存"]}
    first = await submit_analysis(db_session, ["缓存热点"], clues=[clue])
    second = await submit_analysis(db_session, ["缓存热点"])
    async def clue_of(run_id: str):
        return json.loads((await db_session.execute(text(
            "SELECT clue::text FROM run_hotspots WHERE run_id=:r"),
            {"r": run_id})).scalar_one())
    assert await clue_of(second.run_id) == await clue_of(first.run_id)


async def test_run_keyframe_route_checks_identity_and_version(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    cfg, run_id = await _run(db_session, tmp_path)
    frames_dir = Path(cfg.index_dir()) / "keyframes" / "hist"
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame = frames_dir / "kf_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xd9")
    row = (await db_session.execute(text(
        "SELECT rh.id, m.material_id::text AS material_id, m.material_snapshot::text AS snap "
        "FROM run_hotspots rh JOIN run_matches m ON m.run_hotspot_id=rh.id "
        "WHERE rh.run_id=:r ORDER BY m.rank LIMIT 1"), {"r": run_id})).mappings().one()
    snapshot = json.loads(row["snap"])
    snapshot["keyframes"] = [str(frame)]
    await db_session.execute(text(
        "UPDATE materials SET keyframes=CAST(:kf AS jsonb) WHERE id=:id"),
        {"kf": json.dumps([str(frame)]), "id": row["material_id"]})
    await db_session.execute(text(
        "UPDATE run_matches SET material_snapshot=CAST(:snap AS jsonb) "
        "WHERE run_hotspot_id=:rh AND material_id=:mid"),
        {"snap": json.dumps(snapshot), "rh": row["id"], "mid": row["material_id"]})
    await db_session.commit()

    response = await get_run_candidate_keyframe(
        run_id, 1, 1, 0, db_session, cfg)
    assert Path(response.path) == frame

    await db_session.execute(text(
        "UPDATE materials SET fingerprint='changed' WHERE id=:id"), {"id": row["material_id"]})
    await db_session.commit()
    with pytest.raises(ConflictError):
        await get_run_candidate_keyframe(run_id, 1, 1, 0, db_session, cfg)

    await db_session.execute(text(
        "UPDATE materials SET fingerprint=:fp WHERE id=:id"),
        {"fp": snapshot["fingerprint"], "id": row["material_id"]})
    await db_session.execute(text("DELETE FROM materials WHERE id=:id"), {"id": row["material_id"]})
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await get_run_candidate_keyframe(run_id, 1, 1, 0, db_session, cfg)


async def test_material_edit_during_explain_keeps_retrieved_snapshot(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_library(db_session)
    cfg = write_config(tmp_path)
    run_id = await start_run(db_session, ["某明星打球场被拍，反差感拉满"])
    real_explain = analysis_workflow.explain_candidates

    async def edit_then_explain(clue, candidates, caller):
        await db_session.execute(text(
            "UPDATE materials SET title='解释期间改的标题' "
            "WHERE path=(SELECT path FROM materials ORDER BY path LIMIT 1)"))
        await db_session.commit()
        return await real_explain(clue, candidates, caller)

    monkeypatch.setattr(analysis_workflow, "explain_candidates", edit_then_explain)
    await run_for(db_session, run_id, cfg, provider=FakeProvider())
    detail = await load_run(db_session, uuid.UUID(run_id), cfg)
    titles = [cand["material"]["title"] for cand in detail["hotspots"][0]["candidates"]]
    assert titles and all(title != "解释期间改的标题" for title in titles)


async def test_terminal_run_rejects_late_different_write(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    cfg, run_id = await _run(db_session, tmp_path)
    detail = await load_run(db_session, uuid.UUID(run_id), cfg)
    hotspot = detail["hotspots"][0]
    recorder = RunRecorder(db_session, uuid.UUID(run_id), cfg)
    with pytest.raises(RuntimeError):
        await recorder.hotspot_finished(
            1, clue={"why_it_works": ["迟到不同结果"]},
            coverage=hotspot["coverage"], candidates=hotspot["candidates"],
            draft=hotspot["draft"], status="succeeded")


async def test_two_runs_keep_their_own_clues_and_drafts(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    await seed_library(db_session)
    cfg = write_config(tmp_path)
    raw = "同一热点两条线索"
    clue_a = {"hotspot_raw": raw, "why_it_works": ["A"],
              "mechanisms": [], "elements": [], "match_keywords": ["A"]}
    clue_b = {"hotspot_raw": raw, "why_it_works": ["B"],
              "mechanisms": [], "elements": [], "match_keywords": ["B"]}
    first = await submit_analysis(db_session, [raw], clues=[clue_a])
    await run_for(db_session, first.run_id, cfg, provider=FakeProvider())
    first_detail = await load_run(db_session, uuid.UUID(first.run_id), cfg)
    first_draft = first_detail["hotspots"][0]["draft"]

    second = await submit_analysis(db_session, [raw], clues=[clue_b])
    await run_for(db_session, second.run_id, cfg, provider=FakeProvider())
    first_again = await load_run(db_session, uuid.UUID(first.run_id), cfg)
    second_detail = await load_run(db_session, uuid.UUID(second.run_id), cfg)

    assert first_again["hotspots"][0]["clue"]["match_keywords"] == ["A"]
    assert second_detail["hotspots"][0]["clue"]["match_keywords"] == ["B"]
    assert first_again["hotspots"][0]["draft"] == first_draft

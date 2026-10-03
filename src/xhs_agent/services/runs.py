"""运行记录的读写：提交分析任务、读取任务状态与运行结果（数据契约 §3.3–§3.5）。

用途：把 `runs` / `hotspots` / `run_hotspots` / `run_matches` 四张表的读写收在一处，
      并按 `openapi.yaml` 组装 `JobStatus` / `RunDetail` / 报告 model 的形状。
输入：`AsyncSession`、热点文本列表（可选 `topk` / `request_id` / 投递器）。
输出：`Submission`（job_id + run_id）；查询类函数查不到时返回 `None`，由接口层折成 404。

范围（S3.3）：只写 `hotspots`（按原文去重复用）+ `runs`（`queued`）+ `run_hotspots` 骨架；
分析结果的写回（`run_matches`、统计、`prompt_versions`）与 ADR 0011 的 RunStore 改造归 S3.4a。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..db.models import Hotspot, Material, Run, RunHotspot, RunMatch
from ..tools import gaps as gaps_tool
from .reporting import build_report_model
from .retrieval import vector_coverage

# 运行状态（数据契约 §5 的枚举）
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
_DONE_STATUSES = (SUCCEEDED, FAILED)
# 未完成状态：接口层据此把"取结果/报告"折成 409
UNFINISHED_STATUSES = (QUEUED, RUNNING)

TOPK_RANGE = (1, 20)


@dataclass(frozen=True)
class Submission:
    """提交结果：轮询用 `job_id`，取结果用 `run_id`。"""

    job_id: str
    run_id: str


def _iso(value: datetime | None) -> str | None:
    """timestamptz → RFC 3339 字符串（契约要求 UTC）。"""
    return value.isoformat() if value else None


def _normalize(hotspots: Sequence[str]) -> list[str]:
    """去空白、去空项、按首次出现顺序去重（同一句热点复用同一条线索，避免重复付费）。"""
    out: list[str] = []
    for raw in hotspots:
        text = str(raw or "").strip()
        if text and text not in out:
            out.append(text)
    return out


async def submit_analysis(session: AsyncSession, hotspots: Sequence[str], *,
                          topk: int = 5, request_id: str = "",
                          enqueue: Callable[[str], Awaitable[None]] | None = None) -> Submission:
    """落库一个待执行的运行：复用/新建 hotspots → 建 runs(queued) → 建 run_hotspots 骨架 → 投递。

    输入：`hotspots`（1 条以上，本函数只做去空白去重，长度与条数由接口层按契约校验）、
    `topk`（1–20，落 `runs.topk` 供 worker 检索时读取）、`request_id`（`X-Request-ID`，可空）、
    `enqueue`（`job_id -> None` 的异步投递器；None = 不投递，测试用）。
    输出：`Submission`。
    异常：投递失败**整体回滚**，不留"永远排队的脏行"，异常原样上抛给接口层折成 503。
    """
    raws = _normalize(hotspots)
    if not raws:
        raise ValueError("至少需要一个非空的热点原文")
    low, high = TOPK_RANGE
    if not low <= topk <= high:
        raise ValueError(f"topk 必须在 {low}–{high} 之间")

    existing = {row.raw_text: row for row in (await session.execute(
        select(Hotspot).where(Hotspot.raw_text.in_(raws)))).scalars().all()}
    hotspot_rows: list[Hotspot] = []
    for raw in raws:
        row = existing.get(raw)
        if row is None:
            row = Hotspot(raw_text=raw, clue={})   # 线索由 worker 的拆解节点回填
            session.add(row)
            existing[raw] = row
        hotspot_rows.append(row)
    await session.flush()

    run = Run(job_id=str(uuid.uuid4()), status=QUEUED, topk=topk,
              request_id=request_id or None)
    session.add(run)
    await session.flush()

    for position, hotspot in enumerate(hotspot_rows, start=1):
        session.add(RunHotspot(run_id=run.id, hotspot_id=hotspot.id, position=position,
                               status=QUEUED, coverage={}))
    await session.flush()

    try:
        if enqueue is not None:
            await enqueue(run.job_id)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return Submission(job_id=run.job_id, run_id=str(run.id))


async def _run_row(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    return (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()


async def load_job(session: AsyncSession, job_id: str) -> dict[str, Any] | None:
    """`JobStatus` 形状：状态、进度（已完成热点数 / 热点总数）与失败原因。"""
    run = (await session.execute(
        select(Run).where(Run.job_id == job_id))).scalar_one_or_none()
    if run is None:
        return None
    total = (await session.execute(select(func.count()).select_from(RunHotspot)
                                   .where(RunHotspot.run_id == run.id))).scalar_one()
    done = (await session.execute(select(func.count()).select_from(RunHotspot)
                                  .where(RunHotspot.run_id == run.id,
                                         RunHotspot.status.in_(_DONE_STATUSES)))).scalar_one()
    payload: dict[str, Any] = {
        "job_id": run.job_id,
        "run_id": str(run.id),
        "status": run.status,
        "progress": round(done / total, 4) if total else 0.0,
        "current_step": None,
    }
    if run.status == FAILED and run.error:
        payload["error"] = {"code": "internal_error", "message": run.error,
                            "detail": {"run_id": str(run.id)}}
    return payload


async def _matches_by_run_hotspot(session: AsyncSession,
                                  run_hotspot_ids: list[uuid.UUID]) -> dict[str, list[dict[str, Any]]]:
    """按 `run_hotspots.id` 分组返回候选（含素材摘要，契约的 `MatchCandidate` 形状）。"""
    if not run_hotspot_ids:
        return {}
    rows = (await session.execute(
        select(RunMatch, Material)
        .join(Material, Material.id == RunMatch.material_id)
        .where(RunMatch.run_hotspot_id.in_(run_hotspot_ids))
        .order_by(RunMatch.run_hotspot_id, RunMatch.rank))).all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for match, material in rows:
        grouped.setdefault(str(match.run_hotspot_id), []).append({
            "rank": match.rank,
            "material_id": str(match.material_id),
            "score": float(match.score),
            "recall_sources": list(match.recall_sources or []),
            "hits": list(match.hits or []),
            "missing": list(match.missing or []),
            "reasons": list(match.reasons or []),
            "usage": match.usage,
            "material": {
                "id": str(material.id),
                "path": material.path,
                "type": material.type,
                "title": material.title,
                "description": material.description,
                "tags": list(material.tags or []),
            },
        })
    return grouped


async def load_run(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any] | None:
    """`RunDetail` 形状：运行汇总 + 每个热点的线索、覆盖度、候选与文稿。"""
    run = await _run_row(session, run_id)
    if run is None:
        return None
    pairs = (await session.execute(
        select(RunHotspot, Hotspot)
        .join(Hotspot, Hotspot.id == RunHotspot.hotspot_id)
        .where(RunHotspot.run_id == run.id)
        .order_by(RunHotspot.position))).all()
    candidates = await _matches_by_run_hotspot(session, [rh.id for rh, _ in pairs])
    hotspots = [{
        "hotspot_id": str(hotspot.id),
        "hotspot_raw": hotspot.raw_text,
        "clue": dict(hotspot.clue or {}),
        "coverage": dict(run_hotspot.coverage or {}),
        "candidates": candidates.get(str(run_hotspot.id), []),
        "draft": run_hotspot.draft,
    } for run_hotspot, hotspot in pairs]
    return {
        "run_id": str(run.id),
        "status": run.status,
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
        "totals": {
            "llm_calls": run.llm_calls,
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "cost_cny": float(run.cost_cny),
            "latency_ms": run.latency_ms,
        },
        "prompt_versions": dict(run.prompt_versions or {}),
        "hotspots": hotspots,
    }


async def load_report_model(session: AsyncSession, cfg: AppConfig,
                            run_id: uuid.UUID) -> dict[str, Any] | None:
    """报告 model（`tools/report.py` 的输入形状）；查不到返回 `None`。

    报告在库里即时组装（ADR 0011：DB 是权威源），缺口建议现场用规则表重算，
    向量覆盖率复用检索服务的口径。
    """
    run = await _run_row(session, run_id)
    if run is None:
        return None
    detail = await load_run(session, run_id)
    if detail is None:  # pragma: no cover - 与上一行同一行数据，理论上不会发生
        return None
    coverage = await vector_coverage(session, cfg)
    hotspots = [dict(item, gap_advice=gaps_tool.gap_advice(item.get("coverage") or {}))
                for item in detail["hotspots"]]
    errors = [run.error] if run.status == FAILED and run.error else []
    return build_report_model(
        run_id=str(run.id),
        created_at=_iso(run.created_at) or "",
        materials_count=coverage.total,
        config={"llm": {"provider": cfg.llm.provider, "model": cfg.llm.model},
                "materials_dir": cfg.materials_dir()},
        hotspots=hotspots,
        totals=detail["totals"],
        errors=errors,
        vector_coverage=coverage.to_dict(),
    )

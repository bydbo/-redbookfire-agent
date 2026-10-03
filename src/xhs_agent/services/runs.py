"""运行记录的读写：提交分析任务、读取任务状态与运行结果（数据契约 §3.3–§3.5）。

用途：把 `runs` / `hotspots` / `run_hotspots` / `run_matches` 四张表的读写收在一处，
      并按 `openapi.yaml` 组装 `JobStatus` / `RunDetail` / 报告 model 的形状。
输入：`AsyncSession`、热点文本列表（可选 `topk` / `request_id` / 投递器）。
输出：`Submission`（job_id + run_id）；查询类函数查不到时返回 `None`，由接口层折成 404。

范围：
- S3.3（提交与读取）：`hotspots` 按原文去重复用 + `runs`（`queued`）+ `run_hotspots` 骨架；
  `load_job` / `load_run` / `load_report_model` 从库里组装契约形状。
- S3.4a（写回）：`RunRecorder` 逐热点落库（`run_hotspots` / `run_matches` / `hotspots.clue`），
  收尾写 `runs` 的状态、统计与 `prompt_versions`；ADR 0011 的 RunStore 改造同期落地。

不做（S3.4b）：Celery 接线、软/硬超时、索引新鲜度检查与"已成功即跳过"的断点续跑。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..db.models import Hotspot, Material, Run, RunHotspot, RunMatch
from ..schemas import SchemaError
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


# ---------------------------------------------------------------------------
# S3.4a · 分析结果写回（Repository 层；ADR 0011：DB 是运行状态的唯一权威源）
# ---------------------------------------------------------------------------


def match_row_values(candidate: dict[str, Any]) -> dict[str, Any]:
    """终态候选 dict（`MatchCandidate.to_dict()`）→ `run_matches` 的字段值。

    输入：候选字典（含 `rank` / `score` / `recall_sources` / `hits` / `missing` /
          `reasons` / `usage`，外加用于排障的 `material_id`）。输出：喂给 `RunMatch(**值)`
          的字段（**不含外键**，外键由调用方补）。
    异常：`reasons` 为空或 `rank` 越界即抛 `SchemaError`——"不允许无理由候选"在 Pydantic
          侧的把关；数据库侧另有 `jsonb_array_length(reasons) >= 1` 与 `rank >= 1` 的 CHECK 兜底。
    """
    reasons = [str(item) for item in (candidate.get("reasons") or []) if str(item).strip()]
    if not reasons:
        raise SchemaError(f"候选素材 {candidate.get('material_id') or '?'} 缺少 reasons")
    rank = int(candidate.get("rank") or 0)
    if rank < 1:
        raise SchemaError(f"候选素材 {candidate.get('material_id') or '?'} 的 rank 必须 >= 1")
    return {
        "rank": rank,
        "score": float(candidate.get("score") or 0.0),
        "recall_sources": [str(item) for item in (candidate.get("recall_sources") or [])],
        "hits": list(candidate.get("hits") or []),
        "missing": list(candidate.get("missing") or []),
        "reasons": reasons,
        "usage": str(candidate.get("usage") or ""),
    }


def _candidate_material_id(candidate: dict[str, Any]) -> uuid.UUID:
    """取候选里的素材 uuid（`material_id` 或 `material.id`），缺失即报错。"""
    raw = candidate.get("material_id") or (candidate.get("material") or {}).get("id") or ""
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"候选素材缺少可解析的 material_id：{raw!r}") from exc


async def planned_hotspots(session: AsyncSession,
                           run_id: uuid.UUID | str) -> list[tuple[int, str]]:
    """按 `position` 读本次运行要分析的热点原文（worker 与写回用例的对齐入口）。

    输入：`session`、`run_id`。输出：`[(position, raw_text), ...]`，与 S3.3 建骨架时的顺序一致。
    """
    rows = (await session.execute(
        select(RunHotspot.position, Hotspot.raw_text)
        .join(Hotspot, Hotspot.id == RunHotspot.hotspot_id)
        .where(RunHotspot.run_id == uuid.UUID(str(run_id)))
        .order_by(RunHotspot.position))).all()
    return [(int(row.position), str(row.raw_text)) for row in rows]


class RunRecorder:
    """一次运行在库里的写回句柄（每跑完一个热点就落一次库）。

    用途：让 `JobStatus.progress` 能由"已完成热点数 / 热点总数"推导；批次收尾再写 `runs`
          的状态与统计。写回是**先删后插**，因此同一 run 重跑按 `(run_hotspot_id, rank)` 幂等。
    输入：`AsyncSession` 与 `run_id`（S3.3 已建好的 `runs` 行）。
    输出：无（就地写库）。每个写方法内部提交，失败回滚后原样上抛——不静默吞错。
    """

    def __init__(self, session: AsyncSession, run_id: uuid.UUID | str) -> None:
        self.session = session
        try:
            self.run_id = uuid.UUID(str(run_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"run_id 不是合法 uuid：{run_id!r}") from exc

    async def _run(self) -> Run:
        run = (await self.session.execute(
            select(Run).where(Run.id == self.run_id))).scalar_one_or_none()
        if run is None:
            raise ValueError(f"run 不存在：{self.run_id}")
        return run

    async def _commit(self) -> None:
        try:
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            raise

    async def total(self) -> int:
        """本次运行的热点总数（`run_hotspots` 行数）。"""
        return int((await self.session.execute(
            select(func.count()).select_from(RunHotspot)
            .where(RunHotspot.run_id == self.run_id))).scalar_one())

    async def topk(self) -> int:
        """本次运行的截断上限（来自提交时的 `topk`）。"""
        return int((await self._run()).topk)

    async def start(self) -> None:
        """开跑：`runs.status='running'` 并记 `started_at`。"""
        run = await self._run()
        run.status = RUNNING
        run.started_at = datetime.now(UTC)
        run.error = None
        await self._commit()

    async def load_clue(self, raw_text: str) -> dict[str, Any] | None:
        """读该热点已有的线索快照；非空即复用（`{}` 视为没线索，返回 None）。"""
        clue = (await self.session.execute(
            select(Hotspot.clue)
            .join(RunHotspot, RunHotspot.hotspot_id == Hotspot.id)
            .where(RunHotspot.run_id == self.run_id, Hotspot.raw_text == raw_text)
            .limit(1))).scalar_one_or_none()
        return dict(clue) if clue else None

    async def hotspot_finished(self, position: int, *, clue: dict[str, Any],
                               coverage: dict[str, Any],
                               candidates: list[dict[str, Any]],
                               draft: dict[str, Any] | None, status: str,
                               error: str = "") -> None:
        """把一个热点的结果写回：`hotspots.clue` → `run_hotspots` → `run_matches`。

        `hotspots.clue` 只在拿到非空线索时才写（失败热点可能还没有线索）；
        `run_matches` 先按 `run_hotspot_id` 删干净再按 rank 插入，保证重跑幂等。
        """
        run_hotspot = (await self.session.execute(
            select(RunHotspot).where(RunHotspot.run_id == self.run_id,
                                     RunHotspot.position == position))).scalar_one_or_none()
        if run_hotspot is None:
            raise ValueError(f"run_hotspots 缺少 position={position} 的行")
        if clue:
            hotspot = (await self.session.execute(
                select(Hotspot).where(Hotspot.id == run_hotspot.hotspot_id))).scalar_one()
            hotspot.clue = dict(clue)
        run_hotspot.status = status
        run_hotspot.coverage = dict(coverage or {})
        run_hotspot.draft = dict(draft) if draft else None
        run_hotspot.error = error or None
        await self.session.execute(
            delete(RunMatch).where(RunMatch.run_hotspot_id == run_hotspot.id))
        for candidate in candidates or []:
            values = match_row_values(candidate)
            self.session.add(RunMatch(run_hotspot_id=run_hotspot.id,
                                      material_id=_candidate_material_id(candidate),
                                      **values))
        await self._commit()

    async def finish(self, *, status: str, totals: dict[str, Any],
                     prompt_versions: dict[str, int],
                     errors: list[str]) -> None:
        """批次收尾：写 `runs` 的状态、结束时间、LLM 统计与 `prompt_versions`。"""
        run = await self._run()
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.llm_calls = int(totals.get("llm_calls") or 0)
        run.prompt_tokens = int(totals.get("prompt_tokens") or 0)
        run.completion_tokens = int(totals.get("completion_tokens") or 0)
        run.cost_cny = Decimal(f"{float(totals.get('cost_cny') or 0.0):.4f}")
        run.latency_ms = int(totals.get("latency_ms") or 0)
        run.prompt_versions = {str(key): int(value)
                               for key, value in (prompt_versions or {}).items()}
        run.error = None if status == SUCCEEDED else ("；".join(errors) or "运行失败")
        await self._commit()

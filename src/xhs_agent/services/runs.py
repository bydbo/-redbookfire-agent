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
- S4.1（对账）：`reconcile_stale_runs` 把卡住的 `running` 行标成 `failed`（worker 启动时调用，
  阈值 `task_time_limit_s × 2`），覆盖 worker 崩溃吞任务与硬超时 SIGKILL 的窄场景。

不在本模块：worker 接线、软/硬超时、索引新鲜度前置与对账的触发时机在 `tasks/`（S3.4b / S4.1）；
"已成功热点即跳过"的断点续跑仍未实现。
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core import progress as progress_tool
from ..core.tracing import traced_db
from ..db.models import Hotspot, Material, Run, RunHotspot, RunMatch
from ..schemas import SchemaError
from ..tools.material_paths import resolve_material
from .material_identity import current_material
from .reporting import build_report_model
from .run_snapshots import digest, evidence_meta

logger = logging.getLogger("xhs_agent.runs")

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


@traced_db(
    "db.submit_analysis",
    operation="insert",
    tables=("hotspots", "runs", "run_hotspots"),
)
async def submit_analysis(
    session: AsyncSession,
    hotspots: Sequence[str],
    *,
    topk: int = 5,
    request_id: str = "",
    clues: Sequence[Mapping[str, Any] | None] | None = None,
    context_constraints: Sequence[Mapping[str, Any]] | None = None,
    observation_context: Mapping[str, str] | None = None,
    enqueue: Callable[[str], Awaitable[None]] | None = None,
) -> Submission:
    """落库一个待执行的运行：复用/新建 hotspots → 建 runs(queued) → 建 run_hotspots 骨架 → 投递。

    输入：`hotspots`（1 条以上，本函数只做去空白去重，长度与条数由接口层按契约校验）、
    `topk`（1–20，落 `runs.topk` 供 worker 检索时读取）、`request_id`（`X-Request-ID`，可空）、
    `clues`（S6.7：与 `hotspots` **等长**、单项可为 None 的线索快照；非空项覆盖对应
      `hotspots.clue`，worker 会据此短路拆解节点、不再重复付费。同一原文出现多次时后者胜）、
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
    clue_snapshots = _clue_snapshots(hotspots, clues)

    existing = {
        row.raw_text: row
        for row in (
            await session.execute(select(Hotspot).where(Hotspot.raw_text.in_(raws)))
        )
        .scalars()
        .all()
    }
    hotspot_rows: list[Hotspot] = []
    for raw in raws:
        row = existing.get(raw)
        if row is None:
            row = Hotspot(raw_text=raw, clue={})  # 线索由 worker 的拆解节点回填
            session.add(row)
            existing[raw] = row
        snapshot = clue_snapshots.get(raw)
        if snapshot is not None:
            # S6.7：用户确认/编辑过的线索覆盖旧快照（历史 run 回看显示的线索会随之更新，
            # 当时产出的 run_matches / coverage / draft 仍保留快照）
            row.clue = snapshot
        hotspot_rows.append(row)
    await session.flush()

    run = Run(
        job_id=str(uuid.uuid4()),
        status=QUEUED,
        topk=topk,
        request_id=request_id or None,
        # A05：把本轮适用事实快照随请求固化——worker 撰稿阶段读同一份，不临时猜会话状态
        context_snapshot={
            "constraints": [dict(item) for item in (context_constraints or [])],
            "observation": {key: str(value) for key, value in (observation_context or {}).items()
                            if key in ("session_id", "turn_id", "policy", "tool_call_id")},
        },
    )
    session.add(run)
    await session.flush()

    for position, hotspot in enumerate(hotspot_rows, start=1):
        # E8-01/02：提交事务内复制本次输入线索；请求没给时复制当时的共享缓存，
        # 之后另一运行修改共享热点不会改变本次运行。
        frozen_clue = clue_snapshots.get(hotspot.raw_text) or (
            dict(hotspot.clue) if hotspot.clue else None)
        evidence = {
            "raw_text": evidence_meta(
                origin="runtime", source_ref=f"run:{run.id}/position:{position}",
                completeness="complete", payload=hotspot.raw_text),
            "clue": evidence_meta(
                origin="runtime" if frozen_clue else "missing",
                source_ref=f"run:{run.id}/position:{position}",
                completeness="complete" if frozen_clue else "missing",
                payload=frozen_clue,
                missing_fields=[] if frozen_clue else ["clue"],
            ),
        }
        session.add(
            RunHotspot(
                run_id=run.id,
                hotspot_id=hotspot.id,
                position=position,
                status=QUEUED,
                coverage={},
                raw_text=hotspot.raw_text,
                clue=dict(frozen_clue) if frozen_clue else None,
                evidence_meta=evidence,
            )
        )
    await session.flush()

    try:
        if enqueue is not None:
            await enqueue(run.job_id)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return Submission(job_id=run.job_id, run_id=str(run.id))


def _clue_snapshots(
    hotspots: Sequence[str], clues: Sequence[Mapping[str, Any] | None] | None
) -> dict[str, dict[str, Any]]:
    """把「与 hotspots 等长」的 `clues` 折成 `raw_text -> 线索快照`（S6.7）。

    - `clues` 为 None → 空 dict（行为与不带该字段完全一致）；
    - 长度不一致 → `ValueError`（接口层已按 400 拦过一遍，这里是服务层兜底）；
    - 空文本 / 空线索跳过；同一原文出现多次时**后者胜**；
    - 快照里的 `hotspot_raw` 一律以当前原文为准（调用方可能没带，或带的是旧值）。
    """
    if clues is None:
        return {}
    if len(clues) != len(hotspots):
        raise ValueError(
            f"clues 必须与 hotspots 等长：hotspots {len(hotspots)}、clues {len(clues)}"
        )
    snapshots: dict[str, dict[str, Any]] = {}
    for raw, clue in zip(hotspots, clues, strict=True):
        text = str(raw or "").strip()
        if not text or not isinstance(clue, Mapping) or not clue:
            continue
        snapshots[text] = {**dict(clue), "hotspot_raw": text}
    return snapshots


async def _run_row(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    return (
        await session.execute(select(Run).where(Run.id == run_id))
    ).scalar_one_or_none()


@traced_db("db.set_run_stage", operation="update", tables=("runs",))
async def set_run_stage(
    session: AsyncSession,
    run_id: uuid.UUID | str,
    *,
    stage: str,
    status: str,
    attempt: int = 1,
    retries: int = 0,
    latency_ms: int = 0,
    error_category: str = "",
    source_id: str = "",
    stage_instance_id: str = "",
    source_seq: int = 0,
    progress_revision: int = 0,
    generation: int = 0,
) -> bool:
    """把一次运行的阶段快照写进 `runs.stage`（S7.20）；查不到该 run 返回 `False`。

    合并口径全在 `core/progress.py`：迟到（`attempt` 更小）的更新被忽略、`nodes` 按
    `(stage, attempt)` 去重封顶。只写快照列并提交，**不动** `runs.status` / 统计 / `error`——
    那些仍由 `RunRecorder` 收尾。调用方（worker / 工作流）负责给出阶段键与状态。
    """
    run = (
        await session.execute(select(Run).where(Run.id == uuid.UUID(str(run_id))))
    ).scalar_one_or_none()
    if run is None:
        return False
    current = progress_tool.normalize(run.stage)
    # E7OBS4-03（C6）：worker 重试后的新代次开始后，旧代次的迟到写入直接拒绝——
    # 不能靠消费者端的序号比较兜底，也不能让旧执行覆盖新执行的阶段事实。
    # 这里比 `core/progress.py` 严：本列的写入方只有 worker 自己（都声明代次），
    # 所以"没声明代次（0）"不能当作通行证，它同样可能来自旧执行。
    current_generation = int(current.get("generation") or 0)
    if current_generation and generation < current_generation:
        logger.warning(
            "拒绝旧执行代次的阶段写入：run=%s generation=%s < 当前 %s（stage=%s）",
            run.id,
            generation,
            current_generation,
            stage,
        )
        return False
    if not source_seq:
        source_seq = int(current.get("source_seq") or 0) + 1
    run.stage = progress_tool.apply(
        current,
        stage=stage,
        status=status,
        attempt=attempt,
        retries=retries,
        error_category=error_category,
        latency_ms=latency_ms,
        run_id=str(run.id),
        source_id=source_id or str(run.id),
        stage_instance_id=stage_instance_id or f"{run.id}:{stage}:{attempt}",
        source_seq=source_seq,
        progress_revision=progress_revision,
        generation=generation,
    )
    try:
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return True


@traced_db(
    "db.load_job", operation="select", tables=("runs", "run_hotspots", "hotspots")
)
async def load_job(session: AsyncSession, job_id: str) -> dict[str, Any] | None:
    """`JobStatus` 形状：状态、进度（已完成热点数 / 热点总数）、当前阶段与失败原因。"""
    run = (
        await session.execute(select(Run).where(Run.job_id == job_id))
    ).scalar_one_or_none()
    if run is None:
        return None
    total = (
        await session.execute(
            select(func.count())
            .select_from(RunHotspot)
            .where(RunHotspot.run_id == run.id)
        )
    ).scalar_one()
    done = (
        await session.execute(
            select(func.count())
            .select_from(RunHotspot)
            .where(RunHotspot.run_id == run.id, RunHotspot.status.in_(_DONE_STATUSES))
        )
    ).scalar_one()
    snapshot = progress_tool.normalize(run.stage)
    payload: dict[str, Any] = {
        "job_id": run.job_id,
        "run_id": str(run.id),
        "status": run.status,
        "progress": round(done / total, 4) if total else 0.0,
        # S7.20：阶段快照是"现在在哪个环节"的事实源；旧行（stage 为空对象）两字段仍照旧
        "current_step": progress_tool.current_step(snapshot),
        "stage": snapshot if (snapshot["stage"] or snapshot["nodes"]) else None,
    }
    if run.status == FAILED and run.error:
        payload["error"] = {
            "code": "internal_error",
            "message": run.error,
            "detail": {"run_id": str(run.id)},
        }
    return payload


async def _matches_by_run_hotspot(
    session: AsyncSession, run_hotspot_ids: list[uuid.UUID],
    cfg: AppConfig | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """按 `run_hotspots.id` 分组返回候选（历史摘要 + 当前素材动态状态）。"""
    if not run_hotspot_ids:
        return {}
    rows = (
        await session.execute(
            select(RunMatch, Material)
            .outerjoin(Material, Material.id == RunMatch.material_id)
            .where(RunMatch.run_hotspot_id.in_(run_hotspot_ids))
            .order_by(RunMatch.run_hotspot_id, RunMatch.rank)
        )
    ).all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for match, material in rows:
        snapshot = dict(match.material_snapshot or {}) or None
        current: dict[str, Any] = {
            "exists": material is not None,
            "file_available": None,
            "preview_available": False,
            "reason": None if material is not None else "material_deleted",
            "path": material.path if material is not None else None,
            "title": material.title if material is not None else None,
        }
        if material is not None:
            current["path"] = material.path
            current["title"] = material.title
            keyframes = list(material.keyframes or [])
            if cfg is not None:
                try:
                    media = resolve_material(cfg.materials_dir(), material.path)
                    current["file_available"] = os.path.isfile(media)
                except (OSError, ValueError):
                    current["file_available"] = False
            current["preview_available"] = bool(
                keyframes and current["file_available"] is not False)
            if snapshot and snapshot.get("fingerprint") != (material.fingerprint or ""):
                current["reason"] = "material_version_changed"
                current["preview_available"] = False
            elif not keyframes:
                current["reason"] = "keyframes_missing"
        grouped.setdefault(str(match.run_hotspot_id), []).append(
            {
                "rank": match.rank,
                "material_id": str(match.material_id),
                "score": float(match.score),
                "recall_sources": list(match.recall_sources or []),
                "hits": list(match.hits or []),
                "missing": list(match.missing or []),
                "reasons": list(match.reasons or []),
                "usage": match.usage,
                "material": snapshot,
                "current_material": current,
                "evidence": dict(match.evidence_meta or {}),
            }
        )
    return grouped


def _totals(run: Run) -> dict[str, Any]:
    """`RunTotals` 投影：批次级模型调用与成本统计（`load_run` 与 `list_runs` 共用）。"""
    return {
        "llm_calls": run.llm_calls,
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "cost_cny": float(run.cost_cny),
        "latency_ms": run.latency_ms,
    }


@traced_db(
    "db.load_run",
    operation="select",
    tables=("runs", "run_hotspots", "hotspots", "run_matches", "materials"),
)
async def load_run(session: AsyncSession, run_id: uuid.UUID,
                   cfg: AppConfig | None = None) -> dict[str, Any] | None:
    """`RunDetail` 形状：只读冻结快照；当前素材状态是独立附加信息。

    旧运行缺快照时返回 `material=None` + 缺失证据，不用当前素材补历史，也不在 GET 中回填。
    """
    run = await _run_row(session, run_id)
    if run is None:
        return None
    pairs = (
        await session.execute(
            select(RunHotspot, Hotspot)
            .join(Hotspot, Hotspot.id == RunHotspot.hotspot_id)
            .where(RunHotspot.run_id == run.id)
            .order_by(RunHotspot.position)
        )
    ).all()
    candidates = await _matches_by_run_hotspot(
        session, [rh.id for rh, _ in pairs], cfg=cfg)
    hotspots = [
        {
            "position": run_hotspot.position,
            "hotspot_id": str(hotspot.id),
            "hotspot_raw": run_hotspot.raw_text if run_hotspot.raw_text is not None
            else hotspot.raw_text,
            "clue": dict(run_hotspot.clue) if run_hotspot.clue else None,
            "status": run_hotspot.status,
            "error": run_hotspot.error,
            "coverage": dict(run_hotspot.coverage or {}),
            "retrieval_meta": dict(run_hotspot.retrieval_meta or {}),
            "gap_advice": dict(run_hotspot.gap_advice or {}) or None,
            "candidates": candidates.get(str(run_hotspot.id), []),
            "draft": run_hotspot.draft,
            "evidence": dict(run_hotspot.evidence_meta or {}),
        }
        for run_hotspot, hotspot in pairs
    ]
    return {
        "run_id": str(run.id),
        "status": run.status,
        "created_at": _iso(run.created_at),
        "finished_at": _iso(run.finished_at),
        "totals": _totals(run),
        "prompt_versions": dict(run.prompt_versions or {}),
        "execution_snapshot": dict(run.execution_snapshot or {}),
        "evidence": dict(run.evidence_meta or {}),
        "hotspots": hotspots,
    }


HOTSPOT_PREVIEW_CHARS = 60


def _preview(raw_text: str | None) -> str:
    """首条热点原文预览：超过 60 字截断加省略号（契约 `RunSummary.hotspot_preview`）。"""
    value = raw_text or ""
    if len(value) <= HOTSPOT_PREVIEW_CHARS:
        return value
    return value[:HOTSPOT_PREVIEW_CHARS] + "…"


@traced_db(
    "db.list_runs", operation="select", tables=("runs", "run_hotspots", "hotspots")
)
async def list_runs(
    session: AsyncSession, *, limit: int, offset: int
) -> dict[str, Any]:
    """`RunList` 形状：`created_at` 倒序分页的历史运行摘要（平局按 id 倒序，保证幂等）。

    `total` 是全部运行条数（分页器用），与当前页条数无关；热点数与首条热点预览在页内
    用两条批量查询补齐，避免逐行 N+1。
    """
    total = (await session.execute(select(func.count()).select_from(Run))).scalar_one()
    rows = list(
        (
            await session.execute(
                select(Run)
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )
    run_ids = [run.id for run in rows]

    counts: dict[uuid.UUID, int] = {}
    previews: dict[uuid.UUID, str] = {}
    if run_ids:
        for run_id, count in (
            await session.execute(
                select(RunHotspot.run_id, func.count())
                .where(RunHotspot.run_id.in_(run_ids))
                .group_by(RunHotspot.run_id)
            )
        ).all():
            counts[run_id] = count
        for run_id, raw_text in (
            await session.execute(
                select(RunHotspot.run_id, Hotspot.raw_text)
                .join(Hotspot, Hotspot.id == RunHotspot.hotspot_id)
                .where(RunHotspot.run_id.in_(run_ids), RunHotspot.position == 1)
            )
        ).all():
            previews[run_id] = raw_text

    return {
        "items": [
            {
                "run_id": str(run.id),
                "status": run.status,
                "created_at": _iso(run.created_at),
                "finished_at": _iso(run.finished_at),
                "hotspot_count": counts.get(run.id, 0),
                "hotspot_preview": _preview(previews.get(run.id)),
                "topk": run.topk,
                "totals": _totals(run),
                "error": run.error,
            }
            for run in rows
        ],
        "total": total,
    }


@traced_db("db.load_material_frames", operation="select", tables=("materials",))
async def load_material_frames(
    session: AsyncSession, material_id: uuid.UUID
) -> list[str] | None:
    """素材登记的关键帧绝对路径列表（取帧接口用）；素材不存在返回 `None`。"""
    row = await current_material(session, material_id)
    if row is None:
        return None
    return list(row.keyframes or [])


@traced_db(
    "db.load_report_model",
    operation="select",
    tables=("runs", "run_hotspots", "hotspots", "run_matches", "materials"),
)
async def load_report_model(
    session: AsyncSession, cfg: AppConfig, run_id: uuid.UUID
) -> dict[str, Any] | None:
    """报告 model：只从冻结快照组装，不重算缺口建议或当前向量覆盖率。"""
    run = await _run_row(session, run_id)
    if run is None:
        return None
    detail = await load_run(session, run_id, cfg)
    if detail is None:  # pragma: no cover - 同一行数据
        return None
    snapshot = dict(run.execution_snapshot or {})
    hotspots = [
        dict(item, gap_advice=list((item.get("gap_advice") or {}).get("items") or []))
        for item in detail["hotspots"]
    ]
    vector = {}
    materials_count = 0
    for item in detail["hotspots"]:
        summary = ((item.get("retrieval_meta") or {}).get("index_summary") or {})
        if summary:
            materials_count = int(summary.get("materials_count") or 0)
            vector = dict(summary.get("vector_coverage") or {})
            break
    errors = [run.error] if run.status == FAILED and run.error else []
    workspace = dict(snapshot.get("workspace") or {})
    return build_report_model(
        run_id=str(run.id),
        created_at=_iso(run.created_at) or "",
        materials_count=materials_count,
        config={
            "llm": dict(snapshot.get("llm") or {}),
            "materials_dir": workspace.get("materials_dir", ""),
        },
        hotspots=hotspots,
        totals=detail["totals"],
        errors=errors,
        vector_coverage=vector or None,
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
    reasons = [
        str(item) for item in (candidate.get("reasons") or []) if str(item).strip()
    ]
    if not reasons:
        raise SchemaError(
            f"候选素材 {candidate.get('material_id') or '?'} 缺少 reasons"
        )
    rank = int(candidate.get("rank") or 0)
    if rank < 1:
        raise SchemaError(
            f"候选素材 {candidate.get('material_id') or '?'} 的 rank 必须 >= 1"
        )
    return {
        "rank": rank,
        "score": float(candidate.get("score") or 0.0),
        "recall_sources": [
            str(item) for item in (candidate.get("recall_sources") or [])
        ],
        "hits": list(candidate.get("hits") or []),
        "missing": list(candidate.get("missing") or []),
        "reasons": reasons,
        "usage": str(candidate.get("usage") or ""),
    }


def _candidate_material_id(candidate: dict[str, Any]) -> uuid.UUID:
    """取候选里的素材 uuid（`material_id` 或 `material.id`），缺失即报错。"""
    raw = (
        candidate.get("material_id")
        or (candidate.get("material") or {}).get("id")
        or ""
    )
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"候选素材缺少可解析的 material_id：{raw!r}") from exc


async def planned_hotspots(
    session: AsyncSession, run_id: uuid.UUID | str
) -> list[tuple[int, str]]:
    """按 `position` 读本次运行要分析的热点原文（worker 与写回用例的对齐入口）。

    输入：`session`、`run_id`。输出：`[(position, raw_text), ...]`，与 S3.3 建骨架时的顺序一致。
    """
    rows = (
        await session.execute(
            select(RunHotspot.position, Hotspot.raw_text)
            .join(Hotspot, Hotspot.id == RunHotspot.hotspot_id)
            .where(RunHotspot.run_id == uuid.UUID(str(run_id)))
            .order_by(RunHotspot.position)
        )
    ).all()
    return [(int(row.position), str(row.raw_text)) for row in rows]


class RunRecorder:
    """一次运行在库里的写回句柄（每跑完一个热点就落一次库）。

    用途：让 `JobStatus.progress` 能由"已完成热点数 / 热点总数"推导；批次收尾再写 `runs`
          的状态与统计。写回是**先删后插**，因此同一 run 重跑按 `(run_hotspot_id, rank)` 幂等。
    输入：`AsyncSession` 与 `run_id`（S3.3 已建好的 `runs` 行）。
    输出：无（就地写库）。每个写方法内部提交，失败回滚后原样上抛——不静默吞错。
    """

    def __init__(self, session: AsyncSession, run_id: uuid.UUID | str,
                 cfg: AppConfig | None = None) -> None:
        self.session = session
        if cfg is not None:
            self.cfg = cfg
        try:
            self.run_id = uuid.UUID(str(run_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"run_id 不是合法 uuid：{run_id!r}") from exc

    async def _run(self) -> Run:
        run = (
            await self.session.execute(select(Run).where(Run.id == self.run_id))
        ).scalar_one_or_none()
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
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(RunHotspot)
                    .where(RunHotspot.run_id == self.run_id)
                )
            ).scalar_one()
        )

    async def topk(self) -> int:
        """本次运行的截断上限（来自提交时的 `topk`）。"""
        return int((await self._run()).topk)

    async def constraints(self) -> list[dict[str, Any]]:
        """提交时固化的适用事实快照（A05）；旧行 / 无快照返回空列表。"""
        snapshot = dict((await self._run()).context_snapshot or {})
        items = snapshot.get("constraints")
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    async def context_snapshot(self) -> dict[str, Any]:
        return dict((await self._run()).context_snapshot or {})

    async def set_stage(
        self,
        *,
        stage: str,
        status: str,
        attempt: int = 1,
        retries: int = 0,
        latency_ms: int = 0,
        error_category: str = "",
        source_id: str = "",
        stage_instance_id: str = "",
        source_seq: int = 0,
        progress_revision: int = 0,
        generation: int = 0,
    ) -> None:
        """写本次运行的阶段快照（S7.20）；口径与 DB span 全在模块级 `set_run_stage`。"""
        await set_run_stage(
            self.session,
            self.run_id,
            stage=stage,
            status=status,
            attempt=attempt,
            retries=retries,
            latency_ms=latency_ms,
            error_category=error_category,
            source_id=source_id,
            stage_instance_id=stage_instance_id,
            source_seq=source_seq,
            progress_revision=progress_revision,
            generation=generation,
        )

    @traced_db("db.run_recorder.start", operation="update", tables=("runs",))
    async def start(self, execution: dict[str, Any] | None = None,
                    *, generation: int = 0) -> None:
        """开跑：`runs.status='running'`、`started_at` 与执行配置快照。"""
        run = await self._run()
        current_generation = int((run.stage or {}).get("generation") or 0)
        if current_generation and generation and generation < current_generation:
            raise RuntimeError("旧执行代次的写入被拒绝，请使用当前代次或创建新 run")
        if run.status in _DONE_STATUSES:
            raise RuntimeError("运行已终结，不能重新开跑；重新分析请创建新 run")
        run.status = RUNNING
        run.started_at = datetime.now(UTC)
        run.error = None
        if execution is not None and not run.execution_snapshot:
            run.execution_snapshot = dict(execution)
            run.evidence_meta = {
                **dict(run.evidence_meta or {}),
                "execution": evidence_meta(
                    origin="runtime", source_ref=f"run:{run.id}/execution",
                    completeness="complete", payload=execution),
            }
        await self._commit()

    async def load_clue(self, raw_text: str) -> dict[str, Any] | None:
        """读**本次运行**冻结的线索；共享 `hotspots.clue` 不再是历史读取源。"""
        clue = (
            await self.session.execute(
                select(RunHotspot.clue)
                .where(RunHotspot.run_id == self.run_id,
                       RunHotspot.raw_text == raw_text)
                .limit(1)
            )
        ).scalar_one_or_none()
        return dict(clue) if clue else None

    @traced_db(
        "db.run_recorder.hotspot_finished",
        operation="write",
        tables=("hotspots", "run_hotspots", "run_matches"),
    )
    async def hotspot_finished(
        self,
        position: int,
        *,
        clue: dict[str, Any],
        coverage: dict[str, Any],
        candidates: list[dict[str, Any]],
        draft: dict[str, Any] | None,
        status: str,
        error: str = "",
        gap_advice: list[dict[str, Any]] | None = None,
        retrieval_meta: dict[str, Any] | None = None,
        material_snapshots: dict[str, dict[str, Any]] | None = None,
        generation: int = 0,
    ) -> None:
        """冻结一个热点的结果；终态后相同结果幂等、不同结果拒绝覆盖。"""
        run = await self._run()
        current_generation = int((run.stage or {}).get("generation") or 0)
        if current_generation and generation and generation < current_generation:
            raise RuntimeError("旧执行代次的热点写入被拒绝，不能覆盖已接受快照")
        run_hotspot = (
            await self.session.execute(
                select(RunHotspot).where(
                    RunHotspot.run_id == self.run_id,
                    RunHotspot.position == position,
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if run_hotspot is None:
            raise ValueError(f"run_hotspots 缺少 position={position} 的行")
        new_rows = self._candidate_rows(run_hotspot.id, candidates, material_snapshots)
        if run.status in _DONE_STATUSES:
            if await self._same_hotspot(
                    run_hotspot, clue=clue, coverage=coverage, draft=draft,
                    status=status, error=error, gap_advice=gap_advice,
                    retrieval_meta=retrieval_meta, new_rows=new_rows):
                return
            raise RuntimeError("运行已终结，迟到或不同的结果不能覆盖已接受快照")

        if run_hotspot.raw_text is None:
            legacy_raw = (await self.session.execute(
                select(Hotspot.raw_text).where(Hotspot.id == run_hotspot.hotspot_id)
            )).scalar_one_or_none()
            if legacy_raw is not None:
                run_hotspot.raw_text = str(legacy_raw)
                evidence = dict(run_hotspot.evidence_meta or {})
                evidence["raw_text"] = evidence_meta(
                    origin="cutover_capture", source_ref=f"run:{run.id}/position:{position}",
                    completeness="complete", payload=str(legacy_raw))
                run_hotspot.evidence_meta = evidence

        if clue:
            hotspot = (await self.session.execute(
                select(Hotspot).where(Hotspot.id == run_hotspot.hotspot_id)
            )).scalar_one()
            hotspot.clue = dict(clue)  # 共享缓存可继续复用；历史读取仍只看 run_hotspots.clue
        run_hotspot.status = status
        run_hotspot.coverage = dict(coverage or {})
        run_hotspot.draft = dict(draft) if draft else None
        run_hotspot.error = error or None
        run_hotspot.clue = dict(clue) if clue else None
        run_hotspot.gap_advice = {"items": list(gap_advice)} if gap_advice is not None else None
        run_hotspot.retrieval_meta = dict(retrieval_meta or {})
        meta = dict(run_hotspot.evidence_meta or {})
        meta["clue"] = evidence_meta(
            origin="runtime" if clue else "missing",
            source_ref=f"run:{run.id}/position:{position}",
            completeness="complete" if clue else "missing",
            payload=dict(clue) if clue else None,
            missing_fields=[] if clue else ["clue"],
        )
        meta["coverage"] = evidence_meta(
            origin="runtime", source_ref=f"run:{run.id}/position:{position}",
            completeness="complete" if coverage else "partial", payload=dict(coverage or {}))
        meta["gap_advice"] = evidence_meta(
            origin="runtime" if gap_advice is not None else "missing",
            source_ref=f"run:{run.id}/position:{position}",
            completeness="complete" if gap_advice is not None else "missing",
            payload={"items": list(gap_advice)} if gap_advice is not None else None,
            missing_fields=[] if gap_advice is not None else ["gap_advice"],
        )
        meta["retrieval"] = evidence_meta(
            origin="runtime" if retrieval_meta else "missing",
            source_ref=f"run:{run.id}/position:{position}",
            completeness="complete" if retrieval_meta else "missing",
            payload=dict(retrieval_meta or {}),
            missing_fields=[] if retrieval_meta else ["retrieval_meta"],
        )
        run_hotspot.evidence_meta = meta

        # 先证明旧候选可以被替换，再重建；保留原 rank / score / reasons。
        await self.session.execute(
            delete(RunMatch).where(RunMatch.run_hotspot_id == run_hotspot.id)
        )
        for row in new_rows:
            self.session.add(RunMatch(**row))
        await self._commit()

    @staticmethod
    def _candidate_rows(
        run_hotspot_id: uuid.UUID,
        candidates: list[dict[str, Any]],
        material_snapshots: dict[str, dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        snapshots = material_snapshots or {}
        rows: list[dict[str, Any]] = []
        for candidate in candidates or []:
            values = match_row_values(candidate)
            material_id = _candidate_material_id(candidate)
            snapshot = snapshots.get(str(material_id))
            if snapshot is None:
                fallback = dict(candidate.get("material") or {})
                snapshot = fallback or None
                origin = "runtime"
                completeness = "partial" if fallback else "missing"
                missing_fields = ["material_snapshot"] if not fallback else []
            else:
                origin = "runtime"
                completeness = "complete"
                missing_fields = []
            meta = evidence_meta(
                origin=origin if completeness != "missing" else "missing",
                source_ref=f"candidate:{material_id}",
                completeness=completeness,
                payload=snapshot,
                missing_fields=missing_fields,
            )
            rows.append({
                "run_hotspot_id": run_hotspot_id,
                "material_id": material_id,
                "material_snapshot": snapshot,
                "evidence_meta": {"candidate": meta},
                **values,
            })
        return rows

    async def _same_hotspot(
        self, run_hotspot: RunHotspot, *, clue: dict[str, Any], coverage: dict[str, Any],
        draft: dict[str, Any] | None, status: str, error: str,
        gap_advice: list[dict[str, Any]] | None,
        retrieval_meta: dict[str, Any] | None,
        new_rows: list[dict[str, Any]],
    ) -> bool:
        """判断重投是否与已接受快照逐字段相同（相同幂等，不同拒绝）。"""
        if (run_hotspot.status != status
                or dict(run_hotspot.clue or {}) != (dict(clue) if clue else {})
                or dict(run_hotspot.coverage or {}) != dict(coverage or {})
                or (run_hotspot.draft or None) != (dict(draft) if draft else None)
                or (run_hotspot.error or "") != error
                or (run_hotspot.gap_advice or None)
                != ({"items": list(gap_advice)} if gap_advice is not None else None)
                or dict(run_hotspot.retrieval_meta or {}) != dict(retrieval_meta or {})):
            return False
        existing = (
            await self.session.execute(
                select(RunMatch).where(RunMatch.run_hotspot_id == run_hotspot.id)
            )
        ).scalars().all()
        if len(existing) != len(new_rows):
            return False
        by_id = {str(row.material_id): row for row in existing}
        for expected in new_rows:
            row = by_id.get(str(expected["material_id"]))
            if row is None:
                return False
            if (row.rank != expected["rank"]
                    or Decimal(str(row.score)) != Decimal(str(expected["score"]))
                    or list(row.recall_sources or []) != list(expected["recall_sources"])
                    or list(row.reasons or []) != list(expected["reasons"])
                    or list(row.hits or []) != list(expected["hits"])
                    or list(row.missing or []) != list(expected["missing"])
                    or (row.usage or "") != expected["usage"]
                    or digest(row.material_snapshot or {}) != digest(expected["material_snapshot"] or {})):
                return False
        return True

    @traced_db("db.run_recorder.finish", operation="update", tables=("runs",))
    async def finish(
        self,
        *,
        status: str,
        totals: dict[str, Any],
        prompt_versions: dict[str, int],
        errors: list[str],
        generation: int = 0,
    ) -> None:
        """批次收尾：写状态 / 统计 / prompt 版本，并补齐执行快照后不再改写。"""
        run = await self._run()
        current_generation = int((run.stage or {}).get("generation") or 0)
        if current_generation and generation and generation < current_generation:
            raise RuntimeError("旧执行代次的收尾被拒绝，不能覆盖已接受快照")
        normalized_versions = {
            str(key): int(value) for key, value in (prompt_versions or {}).items()
        }
        if run.status in _DONE_STATUSES:
            same = (
                run.status == status
                and run.llm_calls == int(totals.get("llm_calls") or 0)
                and run.prompt_tokens == int(totals.get("prompt_tokens") or 0)
                and run.completion_tokens == int(totals.get("completion_tokens") or 0)
                and dict(run.prompt_versions or {}) == normalized_versions
            )
            if same:
                return
            raise RuntimeError("运行已终结，迟到或不同的收尾不能覆盖已接受快照")
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.llm_calls = int(totals.get("llm_calls") or 0)
        run.prompt_tokens = int(totals.get("prompt_tokens") or 0)
        run.completion_tokens = int(totals.get("completion_tokens") or 0)
        run.cost_cny = Decimal(f"{float(totals.get('cost_cny') or 0.0):.4f}")
        run.latency_ms = int(totals.get("latency_ms") or 0)
        run.prompt_versions = normalized_versions
        run.error = None if status == SUCCEEDED else ("；".join(errors) or "运行失败")
        if run.execution_snapshot:
            snapshot = {**dict(run.execution_snapshot),
                        "prompt_versions": normalized_versions}
            run.execution_snapshot = snapshot
            meta = dict(run.evidence_meta or {})
            meta["execution"] = evidence_meta(
                origin="runtime", source_ref=f"run:{run.id}/execution",
                completeness="complete", payload=snapshot)
            run.evidence_meta = meta
        await self._commit()


async def mark_run_failed(
    session: AsyncSession, run_id: uuid.UUID | str, error: str
) -> bool:
    """把运行标成 `failed`（兜底路径：异常逃出 `run_analysis`，S3.4b 的任务层调用）。

    只写 `status` / `finished_at` / `error`，**不动**已有的统计与 `prompt_versions`——那些
    可能已被逐热点写回更新过，覆盖成 0 等于丢事实。查不到该 run 时返回 `False`。
    """
    run = (
        await session.execute(select(Run).where(Run.id == uuid.UUID(str(run_id))))
    ).scalar_one_or_none()
    if run is None:
        return False
    run.status = FAILED
    run.finished_at = datetime.now(UTC)
    run.error = str(error)[:2000] or "运行失败"
    try:
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return True


def stale_cutoff(now: datetime, threshold_s: int) -> datetime:
    """陈旧运行的截止点：`started_at` 早于它就算陈旧（阈值 = `task_time_limit_s × 2`）。"""
    return now - timedelta(seconds=int(threshold_s))


def stale_error(threshold_s: int) -> str:
    """陈旧运行的 `error` 文案（对账写回，供 `/api/jobs/{job_id}` 直接展示）。"""
    return (
        f"陈旧运行对账：超过 {int(threshold_s)} 秒未收尾"
        "（worker 崩溃或硬超时），已标记失败"
    )


async def reconcile_stale_runs(
    session: AsyncSession, *, threshold_s: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    """把卡住不动的 `running` 行标成 `failed`（S4.1 的陈旧 run 对账）。

    为什么需要：worker 被 SIGKILL / 硬超时打断时任务被吞，`runs` 会永远停在 `running`，
    轮询接口就一直返回 409。对账按"`started_at` 早于 `now - threshold_s`"判定陈旧，
    覆盖这一窄场景；阈值由调用方给（worker 启动时取 `[queue].task_time_limit_s × 2`）。

    只碰 `running` 且 `started_at` 非空的行——`queued`（可能刚投递还没被消费）与终态一律不动；
    自然幂等：跑第二次返回空清单。返回被标记的行（`run_id` / `job_id` / `started_at`）供日志。
    """
    moment = now or datetime.now(UTC)
    cutoff = stale_cutoff(moment, threshold_s)
    rows = (
        (
            await session.execute(
                select(Run).where(
                    Run.status == RUNNING,
                    Run.started_at.is_not(None),
                    Run.started_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    detail = stale_error(threshold_s)
    reconciled: list[dict[str, Any]] = []
    for run in rows:
        run.status = FAILED
        run.finished_at = moment
        run.error = detail
        reconciled.append(
            {
                "run_id": str(run.id),
                "job_id": run.job_id,
                "started_at": _iso(run.started_at),
            }
        )
    try:
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    return reconciled

"""五节点状态图：拆解 → 检索 → 缺口 → 撰稿 → 报告（S3.1）。

用途：把一次热点的分析串成显式状态图。图处理**单个热点**；多热点由 `run_analysis` 循环，
      共享一个 `RunStore` 与一份 run 级报告（与 `run_hotspots` 的"批次 × 热点"语义一致）。
输入：注入的 `caller`（结构化模型调用）、`retrieve`（异步检索可调用对象）、`RunStore`、基准目录。
输出：`AnalysisState` 快照；`run_analysis` 返回 `RunResult`（含 `prompt_versions` 与报告路径）。

传输层（S3.5）：`run_analysis` 起一个 httpx 客户端（连接池）并注入文本模型与向量客户端；
`caller` / `embedder` 由调用方注入时以注入的为准（离线单测用）。

不做降级：
- 任一节点重试后仍失败即抛错，绝不返回半成品；
- 单个热点失败**不阻断整批**，但会记进 `errors` 与报告（失败热点同样有一条 error 条目）；
- 节点级重试（LangGraph `RetryPolicy`）管上游/网络抖动，与 `StructuredCaller` 的 JSON 自修
  是两层，互不替代。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from ..agents import DEFAULT_STYLE, explain_candidates, extract_clue, write_draft
from ..config import PROJECT_ROOT, AppConfig
from ..schemas import HotspotClue
from ..services.reporting import build_report_model, hotspot_entry, render_and_save
from ..services.retrieval import RetrievalOutcome, retrieve_candidates
from ..services.runs import RunRecorder
from ..tools import gaps as gaps_tool
from ..tools.http import build_http_client
from ..tools.llm import StructuredCaller, build_provider
from ..tools.prompt import PromptError
from ..tools.trace import RunStore
from .state import AnalysisState

# 计划步骤：写进 RunStore.plan，用于 pending_steps() 的未完成步骤计算
NODE_STEPS: tuple[str, ...] = ("拆解", "检索", "缺口", "撰稿", "报告")


def retry_on_error(exc: Exception) -> bool:
    """节点级重试的判定：契约/编程错误不重试，其余（网络、上游、超时）重试。

    LangGraph 的 `default_retry_on` 明确**不重试** `ValueError` / `RuntimeError` /
    `OSError` 这些内建类型，而我们的 `LLMError`（RuntimeError）正是上游抖动的主要载体，
    所以这里显式声明：`PromptError`（缺段缺变量）与 `ValueError`（含 `SchemaError`、
    TypeError 等契约/类型错误）不重试，其余一律重试。
    """
    return not isinstance(exc, (PromptError, ValueError, TypeError, ImportError,
                                LookupError, NameError))


DEFAULT_RETRY = RetryPolicy(max_attempts=3, retry_on=retry_on_error)


@dataclass
class RunResult:
    """一次运行的结果：prompt 版本、报告条目、产物路径与汇总。"""

    run_id: str = ""
    status: str = ""
    prompt_versions: dict[str, int] = field(default_factory=dict)
    hotspots: list[dict] = field(default_factory=list)
    report_paths: dict[str, str] = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"运行 {self.run_id}：{self.status}，热点 {len(self.hotspots)} 条，"
                f"prompt 版本 {self.prompt_versions or '{}'}，"
                f"模型调用 {self.totals.get('llm_calls', 0)} 次，"
                f"成本 {self.totals.get('cost_cny', 0)} 元")


def _merge_version(state: AnalysisState, outcome) -> dict[str, int]:
    """把 agent 实际用到的 prompt 版本并进 state（`version=None` 表示没调模型，不记）。"""
    versions = dict(state.get("prompt_versions") or {})
    if outcome.version is not None:
        versions[outcome.task_id] = outcome.version
    return versions


def _meta_from_retrieval(state: AnalysisState) -> dict:
    """报告 meta 的素材信息：素材总数与向量覆盖率都取自检索结果。"""
    vector = (state.get("retrieval") or {}).get("vector_coverage") or {}
    meta: dict = {}
    if vector.get("total"):
        meta["materials_count"] = vector["total"]
    if vector:
        meta["vector_coverage"] = vector
    return meta


def build_graph(*, caller: StructuredCaller, retrieve: Callable[[HotspotClue], Awaitable],
                store: RunStore, base_dir: str = PROJECT_ROOT,
                style: str = DEFAULT_STYLE, retry_policy: RetryPolicy | None = None):
    """装配五节点图。依赖（模型调用 / 检索 / 运行记录）全部可注入，便于离线单测。"""
    policy = retry_policy if retry_policy is not None else DEFAULT_RETRY

    def _record_llm(before: int) -> None:
        """把本节点新产生的模型调用记录进 RunStore（进度上报）。"""
        for record in caller.records[before:]:
            store.record_llm(record)

    async def clue_node(state: AnalysisState) -> dict:
        # 线索复用：同一个热点第二次分析时直接用库里已存的快照，省掉一次模型调用
        # （数据契约 §3.2「避免重复付费」）。步骤照记，pending 计算不受影响。
        if state.get("clue"):
            with store.step(NODE_STEPS[0], detail="复用已有线索"):
                pass
            return {}
        with store.step(NODE_STEPS[0], detail=state.get("hotspot_raw", "")):
            before = len(caller.records)
            outcome = await extract_clue(state["hotspot_raw"], caller)
            _record_llm(before)
        return {"clue": outcome.value.to_dict(),
                "prompt_versions": _merge_version(state, outcome)}

    async def retrieve_node(state: AnalysisState) -> dict:
        clue = HotspotClue.from_dict(state["clue"], hotspot_raw=state.get("hotspot_raw", ""))
        with store.step(NODE_STEPS[1], detail=clue.hotspot_raw):
            outcome: RetrievalOutcome = await retrieve(clue)
            before = len(caller.records)
            explained = await explain_candidates(clue, list(outcome.candidates), caller)
            _record_llm(before)
        candidates = explained.value if explained.value else list(outcome.candidates)
        return {
            "retrieval": {
                "candidates": [item.to_dict() for item in candidates],
                "coverage": outcome.coverage.to_dict(),
                "vector_coverage": outcome.vector_coverage.to_dict(),
                "literal_recalled": outcome.literal_recalled,
                "vector_recalled": outcome.vector_recalled,
                "candidates_considered": outcome.candidates_considered,
            },
            "prompt_versions": _merge_version(state, explained),
        }

    async def gaps_node(state: AnalysisState) -> dict:
        with store.step(NODE_STEPS[2]):
            coverage = (state.get("retrieval") or {}).get("coverage") or {}
            advice = gaps_tool.gap_advice(coverage)
        return {"gap_advice": advice}

    async def draft_node(state: AnalysisState) -> dict:
        clue = HotspotClue.from_dict(state["clue"], hotspot_raw=state.get("hotspot_raw", ""))
        candidates = (state.get("retrieval") or {}).get("candidates") or []
        chosen = candidates[0].get("material") if candidates else None
        with store.step(NODE_STEPS[3], detail=clue.hotspot_raw):
            before = len(caller.records)
            outcome = await write_draft(clue, chosen, caller, style=style)
            _record_llm(before)
        update: dict = {"draft": outcome.value.to_dict() if outcome.value is not None else None,
                        "prompt_versions": _merge_version(state, outcome)}
        if outcome.value is None:
            update["notes"] = [*(state.get("notes") or []), "本次没有候选素材，跳过文案初稿"]
        return update

    async def report_node(state: AnalysisState) -> dict:
        with store.step(NODE_STEPS[4], detail=state.get("hotspot_raw", "")):
            model = dict(state.get("report_model") or {})
            entry = hotspot_entry(
                clue=state.get("clue") or {},
                coverage=(state.get("retrieval") or {}).get("coverage") or {},
                candidates=(state.get("retrieval") or {}).get("candidates") or [],
                gap_advice=state.get("gap_advice"),
                draft=state.get("draft"),
            )
            updated = {
                **model,
                "meta": {**(model.get("meta") or {}), **_meta_from_retrieval(state)},
                "hotspots": [*(model.get("hotspots") or []), entry],
                "totals": dict(store.state["totals"]),
                "errors": list(state.get("errors") or []),
            }
            markdown_path, html_path = render_and_save(updated, store, base_dir=base_dir)
        return {"report_model": updated,
                "report_paths": {"markdown": markdown_path, "html": html_path}}

    graph = StateGraph(AnalysisState)
    graph.add_node("clue", clue_node, retry_policy=policy)
    graph.add_node("retrieve", retrieve_node, retry_policy=policy)
    graph.add_node("gaps", gaps_node)
    graph.add_node("draft", draft_node, retry_policy=policy)
    graph.add_node("report", report_node)
    graph.add_edge(START, "clue")
    graph.add_edge("clue", "retrieve")
    graph.add_edge("retrieve", "gaps")
    graph.add_edge("gaps", "draft")
    graph.add_edge("draft", "report")
    graph.add_edge("report", END)
    return graph.compile()


async def run_analysis(hotspots: list[str], *, cfg: AppConfig, session, store: RunStore | None = None,
                       caller: StructuredCaller | None = None, embedder=None,
                       now: float | None = None, style: str = DEFAULT_STYLE,
                       retry_policy: RetryPolicy | None = None,
                       run_id: str | uuid.UUID | None = None,
                       topk: int | None = None) -> RunResult:
    """跑一批热点：循环调用五节点图，返回 `RunResult`。

    输入：热点原文列表、`AppConfig`、异步会话；`store` / `caller` / `embedder` 可注入（测试用）。
    `run_id` 传入即进入**写回模式**（ADR 0011）：本批对应库里一条 `runs` 行，热点顺序按
      `run_hotspots.position` 对齐，每个热点跑完立即落库，收尾写 `runs` 的状态与统计；
      不传 `run_id` 时保持纯离线行为（完全不碰库），评测脚本与既有单测沿用这条路径。
    `topk` 显式传入时优先，其次读 `runs.topk`，都没有才用 `cfg.match.topk`。
    输出：`RunResult`——`prompt_versions` 在这里汇总，写回模式下同时已落 `runs.prompt_versions`。
    """
    raws = [str(item).strip() for item in hotspots if str(item or "").strip()]
    if not raws:
        raise ValueError("至少需要一个非空的热点原文")

    recorder = RunRecorder(session, run_id) if run_id is not None else None
    effective_topk = topk
    if effective_topk is None and recorder is not None:
        effective_topk = await recorder.topk()   # 收口 S3.3 的 runs.topk：提交时的 topk 真的影响检索
    if recorder is not None:
        total = await recorder.total()
        if total != len(raws):
            raise ValueError(f"热点数量与 run_hotspots 不一致：传入 {len(raws)}、库里 {total}")
        await recorder.start()

    store = store if store is not None else RunStore(cfg.runs_dir(), slug=raws[0],
                                                     meta={"hotspots": len(raws)},
                                                     run_id=str(run_id) if run_id else None)
    store.plan(list(NODE_STEPS))

    # S3.5：一次运行共享一个 httpx 客户端（连接池），同时注入文本模型与向量客户端；
    # 客户端由本函数持有并关闭——不做进程级单例，Celery 每个任务一个新事件循环。
    async with build_http_client(max(cfg.llm.timeout_s, cfg.embedding.timeout_s)) as http:
        caller = caller if caller is not None else StructuredCaller(
            provider=build_provider(cfg, client=http))

        async def retrieve(clue: HotspotClue):
            # http 透传给检索层：它要在造向量客户端时复用本次运行的连接池（S3.5）
            return await retrieve_candidates(session, cfg, clue, embedder=embedder, now=now,
                                             topk=effective_topk, http=http)

        graph = build_graph(caller=caller, retrieve=retrieve, store=store,
                            style=style, retry_policy=retry_policy)
        model = build_report_model(
            run_id=store.run_id,
            created_at=store.state["created_at"],
            materials_count=0,
            config={"llm": {"provider": cfg.llm.provider, "model": cfg.llm.model},
                    "materials_dir": cfg.materials_dir()},
            hotspots=[], totals={}, errors=[],
        )
        versions: dict[str, int] = {}
        errors: list[str] = []
        failed: list[dict] = []
        report_paths: dict[str, str] = {}

        for position, raw in enumerate(raws, start=1):
            initial: dict = {"hotspot_raw": raw, "report_model": model,
                             "prompt_versions": versions, "errors": errors}
            reused = await recorder.load_clue(raw) if recorder is not None else None
            if reused:
                initial["clue"] = reused      # 预置线索 → 拆解节点短路，不再付费
            try:
                result = await graph.ainvoke(initial)
            except Exception as exc:  # 节点重试后仍失败：记进报告，不阻断整批
                detail = f"{type(exc).__name__}: {exc}"
                errors = [*errors, f"[{raw}] {detail}"]
                failed.append(hotspot_entry(clue=reused or {}, coverage={}, candidates=[],
                                            error=detail))
                if recorder is not None:
                    await recorder.hotspot_finished(position, clue=reused or {}, coverage={},
                                                    candidates=[], draft=None,
                                                    status="failed", error=detail)
                continue
            model = result.get("report_model") or model
            versions = result.get("prompt_versions") or versions
            errors = result.get("errors") or errors
            report_paths = result.get("report_paths") or report_paths
            if recorder is not None:
                await recorder.hotspot_finished(
                    position,
                    clue=result.get("clue") or reused or {},
                    coverage=(result.get("retrieval") or {}).get("coverage") or {},
                    candidates=(result.get("retrieval") or {}).get("candidates") or [],
                    draft=result.get("draft"),
                    status="succeeded",
                )

        if failed:
            model = {**model, "hotspots": [*(model.get("hotspots") or []), *failed],
                     "errors": errors, "totals": dict(store.state["totals"])}
            markdown_path, html_path = render_and_save(model, store, base_dir=PROJECT_ROOT)
            report_paths = {"markdown": markdown_path, "html": html_path}

        status = "succeeded" if not errors else "failed"
        store.finalize(status=status, notes="；".join(errors))
        if recorder is not None:
            await recorder.finish(status=status, totals=dict(store.state["totals"]),
                                  prompt_versions=versions, errors=errors)
        return RunResult(
            run_id=store.run_id,
            status=status,
            prompt_versions=dict(versions),
            hotspots=list(model.get("hotspots") or []),
            report_paths=report_paths,
            totals=dict(store.state["totals"]),
            errors=list(errors),
        )

"""基线对比评测：纯关键词子串基线 vs 双通道混合召回（S2.8）。

用途：在只装 `evals/fixtures/demo_pack` 的**临时 Postgres**（testcontainers）上，用 evals v1 的
      20 组用例跑两臂对照，产出《产品方案》§10.2 的五项指标，并落一份可复现报告。
输入：`config/.env` 的密钥（`DASHSCOPE_API_KEY` 必需，向量臂要真实调用）与冻结的 `evals/` 用例；
      可选 `--limit N`（只跑前 N 组）、`--out DIR`（默认 `evals/reports/`）、`--no-sweep`、
      `--keep-container`（调试时保留容器）。
输出：`evals/reports/retrieval-v1-<日期>.md`（人看的报告）+ 同名 `.json`（原始数字，便于 diff）。
      退出码 0 = 评测完成（即使未达标）、1 = 环境 / 密钥 / 上游失败（不降级）。

口径（详见报告里的「口径说明」）：
- E2 还没有 LLM 拆解，本轮用标注构造 **oracle 线索**（`must ∪ allowed`，统一 schema 默认权重），
  因此数字只反映**检索层**；
- 正确答案 = `must_hit ∪ acceptable`；全缺口用例（`must_hit` 为空）不计入两项命中率的分母，
  但仍计入覆盖度 / 耗时 / 成本；
- 耗时是单次检索（不含首次建索引与向量回填）；成本只含查询向量（检索层无 LLM 调用）。

用法：

    uv run python scripts/eval_retrieval.py --limit 3   # 冒烟
    uv run python scripts/eval_retrieval.py             # 全量 + 阈值扫描
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

import docker
from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.db import Base
from xhs_agent.db.models import Material as MaterialRow
from xhs_agent.schemas import Element, HotspotClue, Material
from xhs_agent.services.materials import (
    backfill_embeddings,
    row_to_material,
    sync_materials,
)
from xhs_agent.services.retrieval import retrieve_candidates
from xhs_agent.tools import matching
from xhs_agent.tools.embedding import EmbeddingError, EmbeddingResult, build_embedder
from xhs_agent.util import normalize_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = PROJECT_ROOT / "evals"
CASES_DIR = EVALS_DIR / "cases"
PACK_DIR = EVALS_DIR / "fixtures" / "demo_pack"
REPORTS_DIR = EVALS_DIR / "reports"
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"
MANIFEST_PATH = EVALS_DIR / "manifest.json"
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"

# 阈值扫描的小网格：默认点 + 逐级放宽（S2.6 实测契约默认阈值下召回为空，见 docs/backlog.md S2.6）
SWEEP_SIMILARITY_THRESHOLDS = (0.2, 0.05, 0.02)
SWEEP_MAX_COSINE_DISTANCES = (0.35, 0.6, 1.0)


# ---------------------------------------------------------------- 纯函数（可单测）


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict]:
    """按文件名顺序读取 `evals/cases/case-*.json`。"""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(cases_dir.glob("case-*.json"))]


def oracle_patterns(case: dict) -> list[str]:
    """用例的标注要素值（`must` + `allowed`），去重去空。"""
    expectations = case["clue_expectations"]
    patterns: list[str] = []
    for item in [*expectations["must"], *expectations["allowed"]]:
        value = str(item.get("value") or "").strip()
        if value and value not in patterns:
            patterns.append(value)
    return patterns


def build_oracle_clue(case: dict) -> HotspotClue:
    """用标注构造 oracle 线索（E2 没有 LLM 拆解）：要素权重/置信度一律走 schema 默认值。"""
    elements = [Element(type=item["type"], value=item["value"])
                for item in [*case["clue_expectations"]["must"],
                             *case["clue_expectations"]["allowed"]]]
    return HotspotClue(hotspot_raw=case["hotspot"], elements=elements,
                       match_keywords=oracle_patterns(case))


def correct_paths(case: dict) -> set[str]:
    """人工标注的正确答案：`must_hit ∪ acceptable`（`must_not` 不计入）。"""
    match = case["match_expectations"]
    return set(match["must_hit"]) | set(match["acceptable"])


def is_full_gap(case: dict) -> bool:
    """全缺口用例：没有 `must_hit`，因此不进两项命中率的分母。"""
    return not case["match_expectations"]["must_hit"]


def relative_pack_path(path: str, pack_dir: Path = PACK_DIR) -> str:
    """把素材绝对路径转成用例里引用的相对路径（统一用 `/`）。"""
    try:
        rel = os.path.relpath(path, pack_dir)
    except ValueError:  # 跨盘符（临时素材目录）
        return path.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def baseline_patterns(clue: HotspotClue) -> list[str]:
    """基线的匹配模式：`match_keywords` + 全部要素值，去重去空。"""
    patterns: list[str] = []
    for text_value in [*clue.match_keywords, *(element.value for element in clue.elements)]:
        cleaned = str(text_value or "").strip()
        if cleaned and cleaned not in patterns:
            patterns.append(cleaned)
    return patterns


def baseline_corpus(material: Material) -> str:
    """基线的匹配对象：文件名（含扩展名）+ 素材文案（标签 / 标题 / 描述）。"""
    return f"{os.path.basename(material.path)} {matching.material_text(material)}"


def keyword_baseline_rank(clue: HotspotClue, materials: list[Material],
                          topk: int) -> list[Material]:
    """纯关键词子串基线（《产品方案》§10.3）。

    输入：oracle 线索 + 素材列表 + Top-K。输出：命中的素材按「命中模式数降序、path 升序」排序；
    一个模式都没命中的素材不进候选。
    """
    patterns = baseline_patterns(clue)
    scored: list[tuple[int, Material]] = []
    for material in materials:
        haystack = normalize_text(baseline_corpus(material))
        hits = sum(1 for pattern in patterns
                   if normalize_text(pattern) and normalize_text(pattern) in haystack)
        if hits:
            scored.append((hits, material))
    scored.sort(key=lambda item: (-item[0], item[1].path))
    return [material for _hits, material in scored[:max(0, topk)]]


def score_case(case: dict, ranked_paths: list[str], *, coverage_ratio: float | None,
               latency_ms: int, cost_cny: float, topk: int) -> dict:
    """单用例判定：Top-5 命中 / 首选命中 / 覆盖度 / 耗时 / 成本。

    `ranked_paths` 是按名次排好的素材相对路径。全缺口用例（无正确答案）的两项命中率记为
    `None`，聚合时自然被排除出分母。
    """
    correct = correct_paths(case)
    full_gap = is_full_gap(case)
    top = ranked_paths[:max(0, topk)]
    scorable = bool(correct) and not full_gap
    return {
        "id": case["id"],
        "scene": case.get("scene", ""),
        "full_gap": full_gap,
        "correct": sorted(correct),
        "ranked": list(ranked_paths),
        "top5_hit": bool(set(top) & correct) if scorable else None,
        "first_hit": bool(ranked_paths[:1] and ranked_paths[0] in correct) if scorable else None,
        "coverage_ratio": coverage_ratio,
        "latency_ms": latency_ms,
        "cost_cny": round(cost_cny, 8),
        "must_not_hits": sorted(set(ranked_paths) & set(case["match_expectations"]["must_not"])),
    }


def _rate(flags: list) -> float | None:
    if not flags:
        return None
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def aggregate_metrics(results: list[dict]) -> dict:
    """把逐用例结果聚合成 §10.2 的五项指标（外加 `must_not` 违规数作为附加观察）。"""
    scored = [item for item in results if item["top5_hit"] is not None]
    coverages = [item["coverage_ratio"] for item in results
                 if item.get("coverage_ratio") is not None]
    latencies = [item["latency_ms"] for item in results]
    costs = [item["cost_cny"] for item in results]
    return {
        "cases": len(results),
        "scored_cases": len(scored),
        "top5_hit_rate": _rate([item["top5_hit"] for item in scored]),
        "first_hit_rate": _rate([item["first_hit"] for item in scored]),
        "mean_coverage": round(statistics.fmean(coverages), 4) if coverages else None,
        "mean_latency_ms": round(statistics.fmean(latencies), 1) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "mean_cost_cny": round(statistics.fmean(costs), 8) if costs else None,
        "must_not_violations": sum(len(item["must_not_hits"]) for item in results),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _delta(hybrid: float | None, baseline: float | None) -> str:
    if hybrid is None or baseline is None:
        return "—"
    return f"{(hybrid - baseline) * 100:+.1f} pp"


# ---------------------------------------------------------------- 运行期工具


class EvalCfg:
    """只覆盖素材路径（与可选的 `[retrieval]`）的 cfg 包装，其余字段透传真实 `AppConfig`。

    评测库是临时容器：连接串由 engine 决定，`AppConfig` 只用来提供参数与密钥。
    """

    def __init__(self, base: AppConfig, materials_dir: str, index_dir: str,
                 retrieval=None) -> None:
        self._base = base
        self._materials_dir = materials_dir
        self._index_dir = index_dir
        self.retrieval = retrieval if retrieval is not None else base.retrieval

    def materials_dir(self) -> str:
        return self._materials_dir

    def index_dir(self) -> str:
        return self._index_dir

    def __getattr__(self, name: str):
        return getattr(self._base, name)


class RecordingEmbedder:
    """缓存 + 记录：同一文本只真正调一次接口（阈值扫描复用它），并累计 token 供成本口径。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.cache: dict[str, list[float]] = {}
        self.calls = 0
        self.prompt_tokens = 0

    def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        for content in texts:
            if content not in self.cache:
                result = self.inner.embed([content])
                self.cache[content] = list(result.vectors[0])
                self.calls += 1
                self.prompt_tokens += result.prompt_tokens
            vectors.append(list(self.cache[content]))
        return EmbeddingResult(vectors=vectors, model=getattr(self.inner, "model", ""),
                               prompt_tokens=0, attempts=1)


def _require_docker() -> None:
    """评测需要可用的 Docker：连不上直接失败并给可照做的提示（不静默跳过）。"""
    try:
        docker.from_env().ping()
    except Exception as exc:  # 连接类异常若干种，统一折成一条中文提示
        raise RuntimeError(
            f"评测需要可用的 Docker：请先启动 Docker Desktop（原始错误：{type(exc).__name__}: {exc}）"
        ) from exc


def _init_statements() -> list[str]:
    body = "\n".join(line for line in
                     (INIT_DIR / "01-extensions.sql").read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("--"))
    return [statement.strip() for statement in body.split(";") if statement.strip()]


async def prepare_schema(engine: AsyncEngine) -> None:
    """建扩展 + 建表：扩展缺失时显式执行同一份初始化 SQL（幂等），仍缺即报错。"""
    async with engine.begin() as conn:
        for statement in _init_statements():
            await conn.execute(text(statement))
        names = set((await conn.execute(text("SELECT extname FROM pg_extension"))).scalars())
        missing = {"vector", "pg_trgm"} - names
        if missing:
            raise RuntimeError(f"评测库缺少扩展：{sorted(missing)}（检查 {INIT_DIR}）")
        await conn.run_sync(Base.metadata.create_all)


async def seed_library(session, eval_cfg: EvalCfg, embedder) -> list[Material]:
    """只装入 demo_pack：先建索引，再用**真实**客户端回填向量。"""
    await sync_materials(session, eval_cfg, vision=None)
    await backfill_embeddings(session, eval_cfg, embedder=embedder)
    rows = list((await session.execute(
        select(MaterialRow).order_by(MaterialRow.path))).scalars().all())
    return [row_to_material(row) for row in rows]


async def run_arms(session, base_cfg: AppConfig, eval_cfg: EvalCfg, cases: list[dict],
                   materials: list[Material], recording: RecordingEmbedder, *,
                   now: float) -> dict:
    """跑两臂：基线（纯关键词子串）与本方案（双通道混合召回）。"""
    topk = base_cfg.match.topk
    price = base_cfg.embedding.price_in_per_m
    baseline_results: list[dict] = []
    hybrid_results: list[dict] = []

    for case in cases:
        clue = build_oracle_clue(case)

        started = time.perf_counter()
        baseline_ranked = [relative_pack_path(material.path)
                           for material in keyword_baseline_rank(clue, materials, topk)]
        baseline_ms = round((time.perf_counter() - started) * 1000, 2)
        baseline_results.append(score_case(case, baseline_ranked, coverage_ratio=None,
                                           latency_ms=baseline_ms, cost_cny=0.0, topk=topk))

        tokens_before = recording.prompt_tokens
        started = time.perf_counter()
        outcome = await retrieve_candidates(session, eval_cfg, clue, embedder=recording, now=now)
        hybrid_ms = round((time.perf_counter() - started) * 1000, 2)
        cost = (recording.prompt_tokens - tokens_before) / 1_000_000 * price
        hybrid_results.append({
            **score_case(case, [relative_pack_path(item.material.path)
                                for item in outcome.candidates],
                         coverage_ratio=outcome.coverage.ratio, latency_ms=hybrid_ms,
                         cost_cny=cost, topk=topk),
            "literal_recalled": outcome.literal_recalled,
            "vector_recalled": outcome.vector_recalled,
        })

    return {"baseline": baseline_results, "hybrid": hybrid_results,
            "baseline_metrics": aggregate_metrics(baseline_results),
            "hybrid_metrics": aggregate_metrics(hybrid_results)}


async def run_sweep(session, base_cfg: AppConfig, eval_cfg: EvalCfg, cases: list[dict],
                    recording: RecordingEmbedder, *, now: float) -> list[dict]:
    """阈值扫描：复用已缓存的查询向量，只给数据与推荐，不改默认值。"""
    topk = base_cfg.match.topk
    rows: list[dict] = []
    for similarity_threshold in SWEEP_SIMILARITY_THRESHOLDS:
        for max_cosine_distance in SWEEP_MAX_COSINE_DISTANCES:
            retrieval = base_cfg.retrieval.model_copy(update={
                "similarity_threshold": similarity_threshold,
                "max_cosine_distance": max_cosine_distance,
            })
            point_cfg = EvalCfg(base_cfg, eval_cfg.materials_dir(), eval_cfg.index_dir(),
                                retrieval=retrieval)
            results: list[dict] = []
            literal_total = 0
            vector_total = 0
            for case in cases:
                clue = build_oracle_clue(case)
                started = time.perf_counter()
                outcome = await retrieve_candidates(session, point_cfg, clue,
                                                    embedder=recording, now=now)
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                literal_total += outcome.literal_recalled
                vector_total += outcome.vector_recalled
                results.append(score_case(case, [relative_pack_path(item.material.path)
                                                 for item in outcome.candidates],
                                          coverage_ratio=outcome.coverage.ratio,
                                          latency_ms=latency_ms, cost_cny=0.0, topk=topk))
            metrics = aggregate_metrics(results)
            rows.append({
                "similarity_threshold": similarity_threshold,
                "max_cosine_distance": max_cosine_distance,
                "is_default": (similarity_threshold == base_cfg.retrieval.similarity_threshold
                               and max_cosine_distance == base_cfg.retrieval.max_cosine_distance),
                "top5_hit_rate": metrics["top5_hit_rate"],
                "first_hit_rate": metrics["first_hit_rate"],
                "mean_coverage": metrics["mean_coverage"],
                "mean_latency_ms": metrics["mean_latency_ms"],
                "literal_recalled": literal_total,
                "vector_recalled": vector_total,
            })
    return rows


async def evaluate(cfg: AppConfig, args) -> dict:
    """完整评测流程：临时库 → 装 demo_pack → 回填向量 → 两臂 → 阈值扫描。"""
    client = build_embedder(cfg)
    if client is None:
        raise ConfigError("向量召回未启用（[embedding].enabled = false）",
                          "评测需要向量通道：把 config/config.toml 的 [embedding].enabled 设为 true")
    recording = RecordingEmbedder(client)
    cases = load_cases()
    if args.limit:
        cases = cases[: max(1, int(args.limit))]

    _require_docker()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="xhs-eval-") as tmp:
        eval_cfg = EvalCfg(cfg, materials_dir=str(PACK_DIR),
                           index_dir=str(Path(tmp) / "index"))
        container = PostgresContainer(POSTGRES_IMAGE, driver="asyncpg", username="xhs",
                                      password="xhs", dbname="xhs")
        if INIT_DIR.is_dir():
            container = container.with_volume_mapping(
                str(INIT_DIR), "/docker-entrypoint-initdb.d", "ro")
        container.start()
        try:
            engine = create_async_engine(container.get_connection_url(), poolclass=NullPool)
            try:
                await prepare_schema(engine)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                async with factory() as session:
                    materials = await seed_library(session, eval_cfg, client)
                    now = time.time()
                    arms = await run_arms(session, cfg, eval_cfg, cases, materials, recording,
                                          now=now)
                    sweep = [] if args.no_sweep else await run_sweep(
                        session, cfg, eval_cfg, cases, recording, now=now)
            finally:
                await engine.dispose()
        finally:
            if not args.keep_container:
                container.stop()

    return {
        "generated_at": dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds"),
        "eval_version": manifest["version"],
        "manifest_frozen_at": manifest.get("frozen_at", ""),
        "cases_total": len(load_cases()),
        "cases_run": len(cases),
        "now": now,
        "environment": {
            "postgres_image": POSTGRES_IMAGE,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "params": {
            "similarity_threshold": cfg.retrieval.similarity_threshold,
            "recall_limit": cfg.retrieval.recall_limit,
            "max_cosine_distance": cfg.retrieval.max_cosine_distance,
            "rrf_k": cfg.retrieval.rrf_k,
            "w_element": cfg.retrieval.w_element,
            "w_rrf": cfg.retrieval.w_rrf,
            "hit_threshold": cfg.retrieval.hit_threshold,
            "topk": cfg.match.topk,
            "min_score": cfg.match.min_score,
            "embedding_model": cfg.embedding.model,
            "embedding_price_in_per_m": cfg.embedding.price_in_per_m,
        },
        "library": {
            "materials_dir": str(PACK_DIR),
            "materials": len(materials),
            "embedding_calls": recording.calls,
            "prompt_tokens": recording.prompt_tokens,
        },
        "arms": arms,
        "sweep": sweep,
    }


# ---------------------------------------------------------------- 报告


def render_markdown(report: dict) -> str:
    params = report["params"]
    baseline = report["arms"]["baseline_metrics"]
    hybrid = report["arms"]["hybrid_metrics"]
    lines = [
        f"# 检索层基线对比报告（evals {report['eval_version']}）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 用例：{report['cases_run']} / {report['cases_total']} 组"
        f"（冻结于 {report['manifest_frozen_at']}）",
        f"- 素材库：`{report['library']['materials_dir']}`，{report['library']['materials']} 条素材，"
        f"查询向量 API 调用 {report['library']['embedding_calls']} 次",
        f"- 环境：{report['environment']['postgres_image']}（临时容器）· "
        f"Python {report['environment']['python']}",
        "",
        "## 一、口径说明",
        "",
        "- **oracle 线索**：E2 还没有 LLM 拆解，本轮用用例标注的 `must ∪ allowed` 要素"
        "（统一 schema 默认权重 0.6 / 置信度 0.7）构造线索，两臂吃同一份线索——"
        "因此数字只反映**检索层**，不衡量拆解质量。",
        "- **正确答案** = `must_hit ∪ acceptable`；`must_not` 不计入正确答案，另行统计违规数。",
        f"- **分母**：全缺口用例（`must_hit` 为空）不计入两项命中率的分母"
        f"（本次分母 {hybrid['scored_cases']} 组），但仍计入覆盖度 / 耗时 / 成本。",
        "- **耗时**：单次检索（不含首次建索引与向量回填）；**成本**：只含查询向量"
        f"（检索层无 LLM 调用，单价 {params['embedding_price_in_per_m']} 元/百万 token）。",
        "- **基线**不建模要素，因此没有覆盖度（记 `—`）、成本恒为 0。",
        "",
        "## 二、主表（两臂对照）",
        "",
        f"生效参数：`similarity_threshold={params['similarity_threshold']}`、"
        f"`max_cosine_distance={params['max_cosine_distance']}`、`recall_limit={params['recall_limit']}`、"
        f"`rrf_k={params['rrf_k']}`、`w_element={params['w_element']}`、`w_rrf={params['w_rrf']}`、"
        f"`hit_threshold={params['hit_threshold']}`、`topk={params['topk']}`、"
        f"`min_score={params['min_score']}`、模型 `{params['embedding_model']}`",
        "",
        "| 指标 | 基线（纯关键词子串） | 本方案（双通道混合召回） | 目标 | 提升 |",
        "| --- | --- | --- | --- | --- |",
        f"| Top-5 命中率 | {_pct(baseline['top5_hit_rate'])} | "
        f"{_pct(hybrid['top5_hit_rate'])} | ≥ 80% | "
        f"{_delta(hybrid['top5_hit_rate'], baseline['top5_hit_rate'])} |",
        f"| 首选命中率 | {_pct(baseline['first_hit_rate'])} | "
        f"{_pct(hybrid['first_hit_rate'])} | ≥ 50% | "
        f"{_delta(hybrid['first_hit_rate'], baseline['first_hit_rate'])} |",
        f"| 线索覆盖度 | — | {_pct(hybrid['mean_coverage'])} | ≥ 60% | — |",
        f"| 单次耗时（均值 / 最大） | {baseline['mean_latency_ms']} / "
        f"{baseline['max_latency_ms']} ms | {hybrid['mean_latency_ms']} / "
        f"{hybrid['max_latency_ms']} ms | ≤ 60 秒 | — |",
        f"| 单次成本（均值） | 0 元 | {hybrid['mean_cost_cny']:.8f} 元 | ≤ 0.05 元 | — |",
        "",
        f"附加观察：`must_not` 违规 {baseline['must_not_violations']}（基线）/ "
        f"{hybrid['must_not_violations']}（本方案）处。",
        "",
    ]

    lines += ["## 三、阈值扫描（只给数据，不改默认值）", ""]
    if report["sweep"]:
        lines += ["| similarity_threshold | max_cosine_distance | Top-5 命中率 | 首选命中率 | "
                  "平均覆盖度 | 字面召回合计 | 向量召回合计 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for row in report["sweep"]:
            mark = " ←默认" if row["is_default"] else ""
            lines.append(
                f"| {row['similarity_threshold']}{mark} | {row['max_cosine_distance']} | "
                f"{_pct(row['top5_hit_rate'])} | {_pct(row['first_hit_rate'])} | "
                f"{_pct(row['mean_coverage'])} | {row['literal_recalled']} | "
                f"{row['vector_recalled']} |")
        lines += ["",
                  "扫描仅用于标定参考：改默认阈值属于《检索契约》§九 口径变更，需要单独决策"
                  "（必要时代码之外另补 ADR）。",
                  f"读法：向量召回合计达到「用例数 × 素材数」= {len(report['arms']['hybrid'])} × "
                  f"{report['library']['materials']} 时，说明该阈值等于**不过滤**"
                  "（所有素材都进候选，RRF 的向量分量退化成常数）；命中率好看但丧失区分度。", ""]
    else:
        lines += ["（本次以 `--no-sweep` 跳过）", ""]

    lines += ["## 四、逐用例明细", "",
              "| 用例 | 场景 | 正确答案 | 基线 Top-5 | 基线首选 | 本方案 Top-5 | 本方案首选 | "
              "覆盖度 | 耗时 ms | 成本 元 | 召回（字面/向量） | 本方案 Top-K 路径 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for base_item, hybrid_item in zip(report["arms"]["baseline"], report["arms"]["hybrid"],
                                      strict=True):
        def flag(value) -> str:
            return "跳过" if value is None else ("✅" if value else "❌")
        lines.append(
            f"| {hybrid_item['id']} | {hybrid_item['scene']} | "
            f"{len(hybrid_item['correct'])} 条 | {flag(base_item['top5_hit'])} | "
            f"{flag(base_item['first_hit'])} | {flag(hybrid_item['top5_hit'])} | "
            f"{flag(hybrid_item['first_hit'])} | {_pct(hybrid_item['coverage_ratio'])} | "
            f"{hybrid_item['latency_ms']} | {hybrid_item['cost_cny']:.8f} | "
            f"{hybrid_item.get('literal_recalled', '—')} / {hybrid_item.get('vector_recalled', '—')} | "
            f"{'、'.join(hybrid_item['ranked']) or '（无候选）'} |")

    lines += ["", "## 五、已知偏差与复现", "",
              "- 本轮不含 LLM 拆解与撰稿，`事实准确率 / 幻觉 / Tool 选择 / 输出结构 / 文案可用率` "
              "五类维度不在本轮（见 `docs/backlog.md` S2.8）。",
              "- **示例素材包的旁车文案是按标注要素写的**，字面基线的匹配模式（要素值）几乎逐字"
              "出现在文件名与说明里，因此基线在本批用例上天然占优；真实素材库的命名与说明不会"
              "这么配合，这个偏差会随真实数据消失。",
              "- `单次耗时` 只算检索；E3 端到端复测时按《产品方案》§10.2 的完整口径重测。",
              "- 阈值若已偏离契约默认，本报告的覆盖度/命中率只在本次参数下成立。",
              "",
              "复现命令：",
              "",
              "```powershell",
              "uv run python scripts/eval_retrieval.py",
              "```",
              ""]
    return "\n".join(lines)


def write_report(report: dict, *, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    date = report["generated_at"][:10]
    md_path = out_dir / f"retrieval-v1-{date}.md"
    json_path = out_dir / f"retrieval-v1-{date}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return md_path, json_path


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检索层基线对比评测（evals v1）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 组用例（冒烟用）")
    parser.add_argument("--out", default=str(REPORTS_DIR), help="报告输出目录")
    parser.add_argument("--no-sweep", action="store_true", help="跳过阈值扫描")
    parser.add_argument("--keep-container", action="store_true", help="跑完保留容器（调试用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_config()
        if not cfg.embedding.enabled:
            raise ConfigError("向量召回未启用（[embedding].enabled = false）",
                              "评测需要向量通道：把 config/config.toml 的 [embedding].enabled 设为 true")
        report = asyncio.run(evaluate(cfg, args))
    except (ConfigError, EmbeddingError) as exc:
        print(f"{getattr(exc, 'code', 'E_EVAL')}: {getattr(exc, 'message', None) or exc}",
              file=sys.stderr)
        fix = getattr(exc, "fix", "")
        if fix:
            print(f"修复：{fix}", file=sys.stderr)
        return 1
    except Exception as exc:  # Docker / 网络 / 数据库：直接失败，不降级
        print(f"E_EVAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    md_path, json_path = write_report(report, out_dir=Path(args.out))
    hybrid = report["arms"]["hybrid_metrics"]
    baseline = report["arms"]["baseline_metrics"]
    print(f"评测完成：{md_path}")
    print(f"原始数字：{json_path}")
    print(f"Top-5 命中率：基线 {_pct(baseline['top5_hit_rate'])} → 本方案 "
          f"{_pct(hybrid['top5_hit_rate'])}；首选命中率：基线 "
          f"{_pct(baseline['first_hit_rate'])} → 本方案 {_pct(hybrid['first_hit_rate'])}")
    print(f"覆盖度 {_pct(hybrid['mean_coverage'])}；耗时均值 {hybrid['mean_latency_ms']} ms；"
          f"成本均值 {hybrid['mean_cost_cny']} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

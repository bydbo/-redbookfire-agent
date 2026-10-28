"""E9 统一命令行：校验、估算、评分、报告与三类检查。

退出码固定（协议 §5.5）：0 通过；2 质量 / 收益未达标；3 环境、配置或数据无效；
4 依赖、预算、执行或人工评分未完成。默认命令不发起外部模型请求。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, AppConfig, load_config
from .budget import BudgetError, PriceTable, price_table_from_config
from .dataset import ValidationReport, load_and_validate
from .decisions import benefit_gate, evidence_gate, quality_gate
from .e2e import E2EResult, validate_e2e
from .metrics import aggregate, case_deltas, paired_bootstrap
from .models import STRATEGIES, Attempt, Dataset, Experiment, HumanScore

EXIT_OK = 0
EXIT_QUALITY = 2
EXIT_INVALID = 3
EXIT_INCOMPLETE = 4
STRATEGY_NAMES = tuple(STRATEGIES)


class CliError(RuntimeError):
    def __init__(self, message: str, code: int = EXIT_INVALID) -> None:
        super().__init__(message)
        self.code = code


def _print_errors(report: ValidationReport) -> None:
    for item in report.errors:
        print(f"ERROR: {item}", file=sys.stderr)
    for item in report.warnings:
        print(f"WARN: {item}", file=sys.stderr)


def _load(root: Path, *, require_frozen: bool) -> tuple[Dataset, ValidationReport]:
    dataset, report = load_and_validate(root, require_frozen=require_frozen)
    if dataset is None:
        _print_errors(report)
        raise CliError("数据包结构无效")
    return dataset, report


def _results_dir(root: Path) -> Path:
    return root / "results"


def read_attempts(root: Path) -> list[Attempt]:
    path = _results_dir(root) / "attempts.jsonl"
    if not path.is_file():
        return []
    attempts: list[Attempt] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            attempts.append(Attempt.model_validate(json.loads(line)))
    return attempts


def write_attempts(root: Path, attempts: Sequence[Attempt]) -> Path:
    path = _results_dir(root) / "attempts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in attempts),
                    encoding="utf-8")
    return path


def read_human_scores(root: Path) -> list[HumanScore]:
    path = _results_dir(root) / "human_scores.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("scores", []) if isinstance(data, dict) else data
    return [HumanScore.model_validate(item) for item in rows]


def _read_experiment(root: Path) -> Experiment | None:
    path = root / "experiment.json"
    if not path.is_file():
        return None
    return Experiment.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _git_commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, check=False, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _config_snapshot(cfg: AppConfig) -> dict[str, Any]:
    return {
        "llm": {"model": cfg.llm.model, "provider": cfg.llm.provider,
                "max_tokens": cfg.llm.max_tokens,
                "price_in_per_m": cfg.llm.price_in_per_m,
                "price_out_per_m": cfg.llm.price_out_per_m},
        "vision": {"enabled": cfg.vision.enabled, "model": cfg.vision.model,
                   "price_in_per_m": cfg.vision.price_in_per_m,
                   "price_out_per_m": cfg.vision.price_out_per_m},
        "embedding": {"enabled": cfg.embedding.enabled, "model": cfg.embedding.model,
                      "dim": cfg.embedding.dim,
                      "price_in_per_m": cfg.embedding.price_in_per_m},
        "retrieval": cfg.retrieval.model_dump(),
        "match": cfg.match.model_dump(),
    }


def _price_table(cfg: AppConfig, path: str | None) -> PriceTable:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        table = PriceTable.from_mapping(data)
        if not table.verified_at or not table.source:
            raise CliError("价格快照必须包含 source 与 verified_at")
        return table
    return price_table_from_config(cfg)


def cmd_validate(args: argparse.Namespace) -> int:
    root = Path(args.experiment)
    dataset, report = load_and_validate(
        root, require_frozen=not args.allow_draft)
    if dataset is None:
        _print_errors(report)
        return EXIT_INVALID
    if args.json:
        print(json.dumps({"counts": report.counts, "errors": report.errors,
                          "warnings": report.warnings}, ensure_ascii=False, indent=2))
    else:
        print(f"数据版本：{dataset.manifest.version}（{dataset.manifest.status}）")
        print(f"计数：{json.dumps(report.counts, ensure_ascii=False)}")
        _print_errors(report)
        if report.ok:
            print("校验通过")
    return EXIT_OK if report.ok else EXIT_INVALID


def cmd_estimate(args: argparse.Namespace) -> int:
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=False)
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    cfg = load_config()
    table = _price_table(cfg, args.prices)
    cases = len(dataset.cases)
    materials = len(dataset.materials)
    material_tokens = int(args.material_tokens)
    # 估算按“预期 token”算，不拿发送前保守预留上界冒充账单；账本仍按上界预留。
    emb_price = cfg.embedding.price_in_per_m
    index = round(materials * material_tokens * emb_price / 1_000_000, 8)
    text_per_call = (1500 * cfg.llm.price_in_per_m
                     + 1000 * cfg.llm.price_out_per_m) / 1_000_000
    decompositions = round(cases * 3 * text_per_call, 8)
    query_vectors = round(cases * 4 * 64 * emb_price / 1_000_000, 8)
    vision_per_call = (3000 * cfg.vision.price_in_per_m
                       + 500 * cfg.vision.price_out_per_m) / 1_000_000
    e2e = round(32 * (text_per_call + vision_per_call
                      + 512 * emb_price / 1_000_000), 8)
    subtotal = index + decompositions + query_vectors + e2e
    total = round(subtotal * 1.2, 8)  # 20% 重试 / 结构修复余量
    budget = float(args.budget)
    print(json.dumps({
        "dataset": dataset.manifest.version,
        "status": dataset.manifest.status,
        "cases": cases,
        "materials": materials,
        "price_source": table.source,
        "price_verified_at": table.verified_at,
        "line_items": {
            "index_embedding": index,
            "150_decompositions": decompositions,
            "query_vectors": query_vectors,
            "32_e2e_attempts": e2e,
            "retry_and_repair_margin": round(total - subtotal, 8),
        },
        "basis": "expected tokens（账本仍按保守上界预留）",
        "total_cny": total,
        "budget_cny": budget,
        "within_budget": total <= budget,
    }, ensure_ascii=False, indent=2))
    if total > budget:
        print(f"估算 {total} 超过预算 {budget}", file=sys.stderr)
        return EXIT_INVALID
    return EXIT_OK


def cmd_freeze_candidate(args: argparse.Namespace) -> int:
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=True)
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    target = root / "experiment.json"
    if target.exists() and not args.force:
        raise CliError(f"{target} 已存在；候选冻结后不能原地覆盖")
    cfg = load_config()
    index_path = _results_dir(root) / "index.json"
    index_digest = ""
    if index_path.is_file():
        index_digest = str(json.loads(index_path.read_text(encoding="utf-8")).get("digest", ""))
    params = _config_snapshot(cfg)
    payload = {
        "experiment_id": args.experiment_id or root.name,
        "data_version": dataset.manifest.version,
        "index_digest": index_digest,
        "candidate_digest": hashlib.sha256(
            json.dumps(params, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "code_commit": _git_commit(),
        "model": cfg.llm.model,
        "prompt_version": "e8-frozen",
        "params": params,
        "time_baseline": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "budget_id": args.budget_id,
    }
    experiment = Experiment.model_validate(payload)
    target.write_text(experiment.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"候选已冻结：{target}")
    return EXIT_OK


def cmd_score(args: argparse.Namespace) -> int:
    root = Path(args.experiment)
    source = Path(args.import_file)
    if not source.is_file():
        raise CliError(f"人工评分文件不存在：{source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    rows = data.get("scores", []) if isinstance(data, dict) else data
    scores = [HumanScore.model_validate(item) for item in rows]
    target = _results_dir(root) / "human_scores.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"scores": [item.model_dump() for item in scores]},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已导入 {len(scores)} 条人工评分 → {target}")
    return EXIT_OK


def _metrics_for(dataset: Dataset, attempts: Sequence[Attempt], *, split: str,
                 strategy: str, clue_mode: str, repeats: int) -> dict[str, Any]:
    cases = [case for case in dataset.cases if case.split == split]
    selected = [attempt for attempt in attempts
                if attempt.strategy == strategy and attempt.clue_mode == clue_mode]
    return aggregate(cases, dataset.qrels, selected, repeats=repeats)


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_report(dataset: Dataset, attempts: Sequence[Attempt],
                  experiment: Experiment | None) -> str:
    lines = [
        "# E9 真实效果评测报告",
        "",
        f"- 数据版本：`{dataset.manifest.version}`（{dataset.manifest.status}）",
        f"- 任务 / 素材：{len(dataset.cases)} / {len(dataset.materials)}",
        f"- 尝试记录：{len(attempts)}",
        f"- 实验候选：`{experiment.experiment_id if experiment else '未冻结'}`",
        "",
    ]
    if not attempts:
        lines += ["## 状态", "", "**not_run**：没有尝试记录，不能报告质量或收益。", ""]
        return "\n".join(lines)
    lines += ["## 检索指标", "",
              "| 线索 | 分区 | 策略 | 任务 | 技术完成 | Top-1 | Hit@5 | nDCG@5 | "
              "误推荐率 | 正确空结果 | P50 ms | P95 ms |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for mode, repeats in (("oracle", 1), ("real", 3)):
        for split in ("dev", "holdout"):
            for strategy in STRATEGY_NAMES:
                metrics = _metrics_for(dataset, attempts, split=split, strategy=strategy,
                                       clue_mode=mode, repeats=repeats)
                lines.append(
                    f"| {mode} | {split} | {strategy} | {metrics['cases']} | "
                    f"{_fmt(metrics['technical_completion'])} | {_fmt(metrics['top1'])} | "
                    f"{_fmt(metrics['hit5'])} | {_fmt(metrics['ndcg'])} | "
                    f"{_fmt(metrics['false_recommendation_rate'])} | "
                    f"{_fmt(metrics['correct_empty_rate'])} | "
                    f"{_fmt(metrics['latency_p50_ms'], 1)} | "
                    f"{_fmt(metrics['latency_p95_ms'], 1)} |")
    lines += ["", "## 说明", "",
              "- 表格只汇总已执行的尝试；缺失任务在技术完成率与检查器里单独报。",
              "- oracle 每任务 1 次；real 每任务 3 次，重复先在任务内平均。",
              "- 未执行 `score` 人工评分、未通过 S8.3 / S7.29 前置时，不得宣称完整实验。",
              ""]
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=False)
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    attempts = read_attempts(root)
    experiment = _read_experiment(root)
    markdown = render_report(dataset, attempts, experiment)
    out = Path(args.out) if args.out else root
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / "report.md"
    json_path = out / "report.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps({
        "dataset": dataset.manifest.version,
        "attempts": len(attempts),
        "experiment": experiment.model_dump() if experiment else None,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{md_path}")
    return EXIT_OK


def _prerequisites(root: Path) -> dict[str, Any]:
    path = root / "prerequisites.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _prereq_ok(root: Path, key: str) -> bool:
    item = _prerequisites(root).get(key, {})
    return bool(isinstance(item, dict) and item.get("complete"))


def _evidence(root: Path, dataset: Dataset, attempts: Sequence[Attempt],
              require_human: bool) -> tuple[int, list[str]]:
    if dataset.manifest.status != "frozen":
        return EXIT_INCOMPLETE, ["数据未冻结"]
    missing: list[str] = []
    for mode, repeats in (("oracle", 1), ("real", 3)):
        selected = [attempt for attempt in attempts if attempt.clue_mode == mode]
        decision = evidence_gate(
            dataset.cases, selected, dataset.qrels, frozen=True, repeats=repeats,
            strategies=STRATEGY_NAMES,
            human_scores=read_human_scores(root), require_human=require_human)
        missing.extend(decision.missing)
    return (EXIT_OK if not missing else EXIT_INCOMPLETE), missing


def _retrieval_gate(root: Path, dataset: Dataset, attempts: Sequence[Attempt],
                    gate: str) -> tuple[int, list[str], dict[str, Any]]:
    if not attempts:
        return EXIT_INCOMPLETE, ["没有检索尝试记录"], {}
    experiment = _read_experiment(root)
    baseline_strategy = "keyword"
    if experiment is not None:
        baseline_strategy = str(experiment.params.get("baseline_strategy", "keyword"))
    hybrid = _metrics_for(dataset, attempts, split="holdout", strategy="hybrid",
                          clue_mode="real", repeats=3)
    baseline = _metrics_for(dataset, attempts, split="holdout", strategy=baseline_strategy,
                            clue_mode="real", repeats=3)
    if gate == "quality":
        decision = quality_gate(hybrid)
        return (EXIT_OK if decision.status == "pass"
                else EXIT_QUALITY if decision.status == "fail"
                else EXIT_INCOMPLETE), decision.reasons, decision.metrics
    holdout = [case for case in dataset.cases
               if case.split == "holdout" and case.expectation.answerable]
    hybrid_rows = {row["case_id"]: row for row in hybrid["rows"]}
    base_rows = {row["case_id"]: row for row in baseline["rows"]}
    missing = [case.case_id for case in holdout
               if case.case_id not in hybrid_rows or case.case_id not in base_rows
               or not hybrid_rows[case.case_id]["complete"]
               or not base_rows[case.case_id]["complete"]
               or hybrid_rows[case.case_id]["ndcg"] is None
               or base_rows[case.case_id]["ndcg"] is None]
    if missing:
        return EXIT_INCOMPLETE, [f"缺少对照任务：{missing[:5]}"], {}
    groups, base_values, hybrid_values = case_deltas(
        holdout,
        {case.case_id: float(base_rows[case.case_id]["ndcg"]) for case in holdout},
        {case.case_id: float(hybrid_rows[case.case_id]["ndcg"]) for case in holdout})
    bootstrap = paired_bootstrap(groups, base_values, hybrid_values)
    decision = benefit_gate(hybrid, baseline, bootstrap)
    return (EXIT_OK if decision.status == "pass"
            else EXIT_QUALITY if decision.status == "fail"
            else EXIT_INCOMPLETE), decision.reasons, decision.metrics


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=args.gate != "evidence")
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    attempts = read_attempts(root)
    if args.gate == "evidence":
        code, reasons = _evidence(root, dataset, attempts, args.require_human)
    elif args.gate in ("quality", "benefit"):
        code, reasons, metrics = _retrieval_gate(root, dataset, attempts, args.gate)
        if metrics:
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        raise CliError(f"未知检查门：{args.gate}")
    for reason in reasons:
        print(f"{args.gate.upper()}: {reason}", file=sys.stderr)
    print(f"{args.gate} 检查退出码：{code}")
    return code


def _live_gate(root: Path, dataset: Dataset, *, stage: str) -> list[str]:
    missing: list[str] = []
    if dataset.manifest.status != "frozen":
        missing.append("数据未冻结")
    if stage in ("retrieval", "e2e") and not _prereq_ok(root, "s8_3_formal_cutover"):
        missing.append("S8.3 正式切换未完成 / 未记录")
    if stage == "e2e" and not _prereq_ok(root, "s7_29_signed"):
        missing.append("S7.29 未签收")
    return missing


def _require_database(args: argparse.Namespace) -> str:
    url = str(getattr(args, "database_url", "") or "").strip()
    if not url:
        raise CliError("必须显式提供 --database-url，禁止默认写真实库")
    os.environ["DATABASE_URL"] = url
    return url


def _migrate(database_url: str) -> bool:
    env = {**os.environ, "DATABASE_URL": database_url, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    if result.returncode:
        print((result.stderr or result.stdout or "")[-1200:], file=sys.stderr)
        return False
    return True


async def _index_async(cfg: AppConfig, dataset: Dataset, root: Path,
                       embedder_factory: Any) -> dict[str, Any]:
    from ..db.session import create_engine_from_config, create_session_factory
    from ..tools.http import build_http_client

    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with build_http_client(cfg.embedding.timeout_s) as http:
            embedder = embedder_factory(http)
            async with factory() as session:
                from .experiment import index_dataset
                return await index_dataset(session, cfg, dataset, root, embedder,
                                           root / "results")
    finally:
        await engine.dispose()


def cmd_index(args: argparse.Namespace) -> int:
    if not args.live:
        print("默认模式禁止外部模型请求；需要显式 --live", file=sys.stderr)
        return EXIT_INVALID
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=True)
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    missing = _live_gate(root, dataset, stage="index")
    if missing:
        for item in missing:
            print(f"BLOCKED: {item}", file=sys.stderr)
        return EXIT_INCOMPLETE
    _require_database(args)
    cfg = load_config()
    if not cfg.embedding.enabled:
        print("index 需要 [embedding].enabled = true", file=sys.stderr)
        return EXIT_INVALID
    if not _migrate(os.environ["DATABASE_URL"]):
        return EXIT_INVALID
    from ..tools.embedding import build_embedder

    payload = asyncio.run(_index_async(
        cfg, dataset, root,
        lambda http: build_embedder(cfg, client=http)))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


async def _retrieval_async(cfg: AppConfig, dataset: Dataset, root: Path,
                           split: str, modes: tuple[str, ...],
                           embedder_factory: Any) -> tuple[list[Any], dict[str, Any]]:
    from ..db.session import create_engine_from_config, create_session_factory
    from ..tools.http import build_http_client
    from ..tools.llm import StructuredCaller, build_provider

    engine = create_engine_from_config(cfg)
    try:
        factory = create_session_factory(engine)
        async with build_http_client(cfg.llm.timeout_s) as http:
            embedder = embedder_factory(http)
            caller = (StructuredCaller(provider=build_provider(cfg, client=http))
                      if "real" in modes else None)
            async with factory() as session:
                from .experiment import run_split
                return await run_split(
                    session, cfg, dataset, root, list(dataset.cases), split=split,
                    embedder=embedder, http=http, caller=caller, modes=modes,
                    now=time.time())
    finally:
        await engine.dispose()


def _merge_attempts(root: Path, dataset: Dataset, split: str,
                    new_attempts: list[Any]) -> None:
    existing = read_attempts(root)
    case_ids = {case.case_id for case in dataset.cases if case.split == split}
    kept = [item for item in existing if item.case_id not in case_ids]
    write_attempts(root, [*kept, *new_attempts])


def cmd_retrieval(args: argparse.Namespace) -> int:
    if not args.live:
        print("默认模式禁止外部模型请求；需要显式 --live", file=sys.stderr)
        return EXIT_INVALID
    if args.sweep and args.split == "holdout":
        print("留出集禁止调参：--sweep 只能用于开发集", file=sys.stderr)
        return EXIT_INVALID
    if args.sweep:
        print("--sweep 尚未接入；先运行固定参数，避免用未经验证的网格冒充选参",
              file=sys.stderr)
        return EXIT_INCOMPLETE
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=True)
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    missing = _live_gate(root, dataset, stage="retrieval")
    if missing:
        for item in missing:
            print(f"BLOCKED: {item}", file=sys.stderr)
        return EXIT_INCOMPLETE
    if not (_results_dir(root) / "index.json").is_file():
        print("缺少 results/index.json；先运行 index", file=sys.stderr)
        return EXIT_INCOMPLETE
    _require_database(args)
    cfg = load_config()
    if not cfg.embedding.enabled:
        print("retrieval 需要 [embedding].enabled = true", file=sys.stderr)
        return EXIT_INVALID
    modes = (("oracle", "real") if args.clue == "both" else (args.clue,))
    from ..tools.embedding import build_embedder

    attempts, index = asyncio.run(_retrieval_async(
        cfg, dataset, root, args.split, modes,
        lambda http: build_embedder(cfg, client=http)))
    _merge_attempts(root, dataset, args.split, attempts)
    summary = {
        "split": args.split,
        "clue_modes": list(modes),
        "attempts": len(attempts),
        "index": index,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    (_results_dir(root) / "retrieval.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK


def _load_e2e(root: Path, path_value: str) -> E2EResult | None:
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return None
    return E2EResult.model_validate(json.loads(path.read_text(encoding="utf-8")))


def cmd_e2e(args: argparse.Namespace) -> int:
    """离线校验已有 32 次证据；live 模式先过前置门，未接入 runner 时返回 4。"""
    root = Path(args.experiment)
    dataset, report = _load(root, require_frozen=False)
    if not report.ok:
        _print_errors(report)
        return EXIT_INVALID
    if args.live:
        missing = _live_gate(root, dataset, stage="e2e")
        if missing:
            for item in missing:
                print(f"BLOCKED: {item}", file=sys.stderr)
            return EXIT_INCOMPLETE
        print("e2e live runner 尚未接入浏览器 / worker；不伪造尝试记录", file=sys.stderr)
        return EXIT_INCOMPLETE
    result = _load_e2e(root, args.results)
    if result is None:
        print(f"没有 E2E 证据：{args.results}", file=sys.stderr)
        return EXIT_INCOMPLETE
    decision = validate_e2e(dataset, result)
    print(json.dumps(decision.model_dump(), ensure_ascii=False, indent=2))
    if decision.status == "pass":
        return EXIT_OK
    return EXIT_QUALITY if decision.status == "fail" else EXIT_INCOMPLETE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E9 真实效果评测")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="校验数据包、hash、分区和 qrel")
    validate.add_argument("--experiment", required=True)
    validate.add_argument("--allow-draft", action="store_true")
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(func=cmd_validate)

    estimate = sub.add_parser("estimate", help="离线估算 10 元预算")
    estimate.add_argument("--experiment", required=True)
    estimate.add_argument("--prices", default=None)
    estimate.add_argument("--budget", type=float, default=10.0)
    estimate.add_argument("--material-tokens", type=int, default=512)
    estimate.set_defaults(func=cmd_estimate)

    freeze = sub.add_parser("freeze-candidate", help="冻结候选代码 / 参数 / 模型")
    freeze.add_argument("--experiment", required=True)
    freeze.add_argument("--experiment-id", default="")
    freeze.add_argument("--budget-id", default="e9-local")
    freeze.add_argument("--force", action="store_true")
    freeze.set_defaults(func=cmd_freeze_candidate)

    score = sub.add_parser("score", help="导入人工评分")
    score.add_argument("--experiment", required=True)
    score.add_argument("--import", dest="import_file", required=True)
    score.set_defaults(func=cmd_score)

    report = sub.add_parser("report", help="生成阶段报告")
    report.add_argument("--experiment", required=True)
    report.add_argument("--out", default=None)
    report.set_defaults(func=cmd_report)

    check = sub.add_parser("check", help="证据 / 质量 / 收益检查")
    check.add_argument("--experiment", required=True)
    check.add_argument("--gate", choices=("evidence", "quality", "benefit"), required=True)
    check.add_argument("--require-human", action="store_true")
    check.set_defaults(func=cmd_check)

    index = sub.add_parser("index", help="在隔离库索引 E9 素材（--live 受控）")
    index.add_argument("--experiment", required=True)
    index.add_argument("--live", action="store_true")
    index.add_argument("--database-url", default="")
    index.add_argument("--max-vision-items", type=int, default=0)
    index.set_defaults(func=cmd_index)

    retrieval = sub.add_parser("retrieval", help="开发集 / 留出集检索对照（--live 受控）")
    retrieval.add_argument("--experiment", required=True)
    retrieval.add_argument("--live", action="store_true")
    retrieval.add_argument("--database-url", default="")
    retrieval.add_argument("--split", choices=("dev", "holdout"), default="dev")
    retrieval.add_argument("--sweep", action="store_true")
    retrieval.add_argument("--clue", choices=("oracle", "real", "both"), default="both")
    retrieval.set_defaults(func=cmd_retrieval)

    e2e = sub.add_parser("e2e", help="校验 20 任务 / 32 次 E2E 证据（--live 受前置门控制）")
    e2e.add_argument("--experiment", required=True)
    e2e.add_argument("--live", action="store_true")
    e2e.add_argument("--results", default="results/e2e.json")
    e2e.set_defaults(func=cmd_e2e)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CliError as exc:
        print(f"E_E9: {exc}", file=sys.stderr)
        return exc.code
    except (BudgetError, ValueError) as exc:
        print(f"E_E9: {exc}", file=sys.stderr)
        return EXIT_INVALID
    except Exception as exc:  # 配置 / 网络 / 数据库失败统一失败，不伪造结果
        print(f"E_E9: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

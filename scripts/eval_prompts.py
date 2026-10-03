"""Prompt 变更回归报告：同一批用例跑新旧两版 prompt，判定七维度硬门槛（S3.10）。

用途：按《prompt 契约》§六 的要求做 prompt 变更前后的对照——固定 `evals/cases/` 的 20 组冻结用例，
      把**被改的那个 prompt** 的新旧两版各跑一遍完整五节点链路，产出逐用例维度变化与门槛结论，
      报告按契约命名落 `evals/reports/prompt-<task_id>-v<N>-<日期>.md`。
输入：`config/.env` 的密钥（文本模型 + 向量都要真调）与冻结的 `evals/`；`--task` 指定本次改的是
      哪个 prompt；`--baseline <git-ref>`（默认 HEAD）作为基线臂的旧正文来源。
输出：报告 md + 同名 json；退出码 0 = 评测跑完（**含判定为回退**，回退是给人看的结论不是脚本故障）、
      1 = 环境 / 密钥 / 上游失败（不降级）。

基线臂怎么来的：`git show <ref>:src/xhs_agent/prompts/<task>.md` 取旧正文，**只在工具层**
把 `prompt_tool.load` 换成"该 task 返回旧正文、其余透传"——不写文件、不改生产代码，
也不违反《prompt 契约》§七"prompt 目录固定在包内、不通过配置切换"。

口径（报告里也会写一遍）：
- 七维度门槛见《prompt 契约》§六，任一不达标即**回退**；
- 「文案可用率」是人工 1–5 分，报告只留列记录、**不进门槛**（契约 §六 明确）；
- 能机械判定的才进门槛：`hallucination_guards`（人工核对）、只在建索引阶段成立的
  `no_vision_when_sidecar` / `vision_when_no_hint`、与本变更无关的 `no_embedding_when_disabled`、
  需要构造坏 JSON 的 `repair_on_bad_json` 一律标 N/A 并写明原因；
- 正确答案 = `must_hit ∪ acceptable`；全缺口用例不进两项命中率的分母（与 S2.8 同口径）。

用法：

    uv run python scripts/eval_prompts.py --task hotspot_clue --limit 2 --out <临时目录>   # 冒烟
    uv run python scripts/eval_prompts.py --task copy_draft --note "收紧口吻约束"
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

import docker
from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.db import Base
from xhs_agent.schemas import Draft, HotspotClue, SchemaError
from xhs_agent.services.materials import (
    backfill_embeddings,
    sync_materials,
)
from xhs_agent.tools import prompt as prompt_tool
from xhs_agent.tools.embedding import EmbeddingError, EmbeddingResult, build_embedder
from xhs_agent.tools.http import build_http_client
from xhs_agent.util import normalize_text
from xhs_agent.workflows import run_analysis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = PROJECT_ROOT / "evals"
CASES_DIR = EVALS_DIR / "cases"
PACK_DIR = EVALS_DIR / "fixtures" / "demo_pack"
REPORTS_DIR = EVALS_DIR / "reports"
INIT_DIR = PROJECT_ROOT / "docker" / "postgres" / "init"
MANIFEST_PATH = EVALS_DIR / "manifest.json"
PROMPTS_DIR = PROJECT_ROOT / "src" / "xhs_agent" / "prompts"

POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg16"
TASKS = ("hotspot_clue", "material_select", "copy_draft")

# 契约 §六 的硬门槛
FACT_ACCURACY_MIN = 0.90
TOP5_MIN = 0.80
FIRST_MIN = 0.50
COST_MAX_CNY = 0.05
LATENCY_MAX_MS = 60_000

_VERSION_RE = re.compile(r"<!--\s*prompt-version:\s*v(\d+)\s*-->")

# 本回合观测不到、只能标 N/A 的 Tool 期望（报告里会写明原因）
TOOL_EXPECTATIONS_NA: dict[str, str] = {
    "no_vision_when_sidecar": "视觉打标只在建索引阶段发生，分析链路里观测不到",
    "vision_when_no_hint": "同上：抽帧与视觉打标属建索引阶段",
    "no_embedding_when_disabled": "需要另跑一条 [embedding].enabled=false 的臂，与 prompt 变更无关",
    "repair_on_bad_json": "需要人为构造坏 JSON 才能触发自修，本次不构造",
}
HALLUCINATION_GUARDS_NOTE = "hallucination_guards 是人工核对项，本报告只给机械判定结果"


class EvalError(RuntimeError):
    """评测环境或输入不满足要求（不降级，直接失败）。"""


# ---------------------------------------------------------------- 纯函数（可单测）


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict]:
    """按文件名顺序读取 `evals/cases/case-*.json`。"""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(cases_dir.glob("case-*.json"))]


def correct_paths(case: dict) -> set[str]:
    """人工标注的正确答案：`must_hit ∪ acceptable`。"""
    match = case["match_expectations"]
    return set(match["must_hit"]) | set(match["acceptable"])


def is_full_gap(case: dict) -> bool:
    """全缺口用例：没有 `must_hit`，不进两项命中率的分母。"""
    return not case["match_expectations"]["must_hit"]


def relative_pack_path(path: str, pack_dir: Path = PACK_DIR) -> str:
    """把素材绝对路径转成用例里引用的相对路径（统一用 `/`）。"""
    try:
        rel = os.path.relpath(path, pack_dir)
    except ValueError:  # 跨盘符
        return path.replace(os.sep, "/")
    return rel.replace(os.sep, "/")


def case_path(path: str) -> str:
    """归一成用例里的相对路径：绝对路径按 demo_pack 取相对，已经是相对的则原样（只统一斜杠）。"""
    text = str(path or "")
    if os.path.isabs(text):
        return relative_pack_path(text)
    return text.replace(os.sep, "/")


def read_prompt_version(text: str) -> int:
    """从 prompt 正文里读 `<!-- prompt-version: vN -->`。"""
    match = _VERSION_RE.search(text or "")
    if not match:
        raise EvalError("prompt 文件缺少 `<!-- prompt-version: vN -->` 头，无法归档报告")
    return int(match.group(1))


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(PROJECT_ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def load_baseline_prompt(ref: str, task: str) -> str:
    """用 `git show` 取基线臂的旧 prompt 正文（不落盘、不改工作区）。"""
    relative = f"src/xhs_agent/prompts/{task}.md"
    result = _git("show", f"{ref}:{relative}")
    if result.returncode != 0:
        raise EvalError(f"取不到基线 prompt：git show {ref}:{relative} 失败"
                        f"（{result.stderr.strip()[:200]}）")
    return result.stdout


def prompt_diff(ref: str, task: str) -> str:
    """本次改动相对基线的 diff（报告里要写明"改的是哪一层"）。"""
    result = _git("diff", ref, "--", f"src/xhs_agent/prompts/{task}.md")
    return result.stdout.strip()


def candidate_prompt_text(task: str) -> str:
    """候选臂正文 = 工作区里的当前文件。"""
    path = PROMPTS_DIR / f"{task}.md"
    if not path.is_file():
        raise EvalError(f"prompt 文件不存在：{path}")
    return path.read_text(encoding="utf-8")


@contextlib.contextmanager
def baseline_prompt(task: str, text: str | None) -> Iterator[None]:
    """把某个 task 的 `prompt_tool.load` 换成返回旧正文（`text=None` 时不动）。

    `render()` 每次都真的 `load()` 且没有缓存，所以这个补丁对整条链路立即生效。
    """
    if text is None:
        yield
        return
    original = prompt_tool.load
    parsed = prompt_tool.parse(text, task_id=task)

    def _load(task_id: str) -> prompt_tool.Prompt:
        return parsed if task_id == task else original(task_id)

    prompt_tool.load = _load        # type: ignore[assignment]
    try:
        yield
    finally:
        prompt_tool.load = original     # type: ignore[assignment]


def _rate(flags: list[bool]) -> float | None:
    if not flags:
        return None
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def element_key(item: dict) -> tuple[str, str]:
    return (str(item.get("type") or ""), str(item.get("value") or ""))


def element_matches(produced: tuple[str, str], expected: tuple[str, str]) -> bool:
    """要素是否算命中：类型必须一致，取值用仓库统一的归一化后「相等或互相包含」。

    为什么不做严格相等：用例标注写 `健身`，模型产出 `健身房撸铁` 属于命中，严格相等会误判。
    """
    if produced[0] != expected[0]:
        return False
    left, right = normalize_text(produced[1]), normalize_text(expected[1])
    if not left or not right:
        return False
    return left == right or left in right or right in left


def score_case_dimensions(case: dict, *, clue: dict, candidates: list[dict],
                          draft: dict | None, error: str = "") -> dict:
    """逐用例的机械判定（纯函数）：命中率 / 幻觉 / 结构 / Tool / 覆盖度。

    「事实准确率」按契约口径算是「`must` 要素命中数 ÷ `must` 总数」（跨用例汇总）；
    `allowed` 覆盖与"产出要素里能回溯的比例"作为观察项一并回传。
    产出为空由**输出结构**那条兜住（`HotspotClue.from_dict` 要求至少一个要素）。
    """
    expectations = case["clue_expectations"]
    must_list = [element_key(item) for item in expectations["must"]]
    allowed_list = [element_key(item) for item in expectations["allowed"]]
    expected = must_list + [item for item in allowed_list if item not in must_list]
    forbidden = {element_key(item) for item in expectations.get("forbidden") or []}

    produced = [element_key(item) for item in (clue.get("elements") or [])]
    grounded = [item for item in produced
                if any(element_matches(item, target) for target in expected)]
    matched_must = [item for item in must_list
                    if any(element_matches(item, produced_item) for produced_item in produced)]
    matched_expected = [item for item in expected
                        if any(element_matches(item, produced_item) for produced_item in produced)]
    precision = round(len(grounded) / len(produced), 4) if produced else None
    forbidden_hits = [item for item in produced if item in forbidden]

    ranked = [case_path(str((candidate.get("material") or {}).get("path") or ""))
              for candidate in candidates]
    must_not = set(case["match_expectations"].get("must_not") or [])
    must_not_hits = [path for path in ranked if path in must_not]
    correct = correct_paths(case)
    full_gap = is_full_gap(case)
    top5_hit = None if full_gap else any(path in correct for path in ranked[:5])
    first_hit = None if full_gap else bool(ranked) and ranked[0] in correct

    structure_problems: list[str] = []
    try:
        HotspotClue.from_dict(clue, hotspot_raw=case["hotspot"])
    except (SchemaError, ValueError) as exc:
        structure_problems.append(f"clue 不合契约：{exc}")
    for index, candidate in enumerate(candidates, start=1):
        if not candidate.get("reasons"):
            structure_problems.append(f"候选 {index} 缺 reasons")
        if not str(candidate.get("usage") or "").strip():
            structure_problems.append(f"候选 {index} 缺 usage")
        if not candidate.get("recall_sources"):
            structure_problems.append(f"候选 {index} 缺 recall_sources")
    if candidates and not draft:
        structure_problems.append("有候选却没有文案初稿")
    if not candidates and draft:
        structure_problems.append("没有候选却产出了文案初稿")
    if draft:
        try:
            top = candidates[0]
            Draft.from_dict(draft, material_id=str(top.get("material_id") or ""),
                            hotspot_key=str(clue.get("hotspot_key") or ""))
        except (SchemaError, ValueError, IndexError) as exc:
            structure_problems.append(f"draft 不合契约：{exc}")

    tool_problems: list[str] = []
    # 通道级判定（评测集 §三）：有候选的用例里，召回来源必须两条通道都出现过
    sources = {source for candidate in candidates
               for source in (candidate.get("recall_sources") or [])}
    if candidates and not sources >= {"literal", "vector"}:
        tool_problems.append(f"recall_both_channels：候选只来自 {sorted(sources)}")
    if full_gap and candidates:
        tool_problems.append("no_candidate_padding：全缺口用例不该硬凑候选")

    return {
        "case": case["id"],
        "error": error,
        "full_gap": full_gap,
        "top5_hit": top5_hit,
        "first_hit": first_hit,
        "grounded_precision": precision,
        "must_matched": len(matched_must),
        "must_total": len(must_list),
        "expected_matched": len(matched_expected),
        "expected_total": len(expected),
        "produced_elements": len(produced),
        "candidates": len(candidates),
        "hallucinations": len(forbidden_hits) + len(must_not_hits),
        "forbidden_hits": [f"{etype}:{value}" for etype, value in forbidden_hits],
        "must_not_hits": must_not_hits,
        "structure_ok": not structure_problems,
        "structure_problems": structure_problems,
        "tool_ok": not tool_problems,
        "tool_problems": tool_problems,
    }


def aggregate_dimensions(results: list[dict]) -> dict:
    """把逐用例结果聚合成报告用的七维度数字。"""
    scored = [item for item in results if item["top5_hit"] is not None]
    precisions = [item["grounded_precision"] for item in results
                  if item.get("grounded_precision") is not None]
    coverages = [item["coverage_ratio"] for item in results
                 if item.get("coverage_ratio") is not None]
    latencies = [item["latency_ms"] for item in results]
    costs = [item["cost_cny"] for item in results]
    must_total = sum(item["must_total"] for item in results)
    expected_total = sum(item["expected_total"] for item in results)
    return {
        "cases": len(results),
        "scored_cases": len(scored),
        "hallucinations": sum(item["hallucinations"] for item in results),
        # 契约口径：must 要素命中数 ÷ must 总数（跨用例汇总）
        "fact_accuracy": (round(sum(item["must_matched"] for item in results) / must_total, 4)
                          if must_total else None),
        "expected_coverage": (round(sum(item["expected_matched"] for item in results)
                                    / expected_total, 4) if expected_total else None),
        "grounded_precision": _mean(precisions),
        "structure_pass_rate": _rate([item["structure_ok"] for item in results]),
        "tool_pass_rate": _rate([item["tool_ok"] for item in results]),
        "top5_hit_rate": _rate([item["top5_hit"] for item in scored]),
        "first_hit_rate": _rate([item["first_hit"] for item in scored]),
        "mean_coverage": _mean(coverages),
        "mean_latency_ms": round(statistics.fmean(latencies), 1) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "mean_cost_cny": round(statistics.fmean(costs), 8) if costs else None,
        "max_cost_cny": round(max(costs), 8) if costs else None,
        "failed_cases": sum(1 for item in results if item["error"]),
    }


def _gate(name: str, threshold: str, baseline: Any, candidate: Any, passed: bool,
          note: str = "", *, gated: bool = True) -> dict:
    return {"dimension": name, "threshold": threshold, "baseline": baseline,
            "candidate": candidate, "passed": passed, "note": note, "gated": gated}


def evaluate_gates(baseline: dict, candidate: dict) -> list[dict]:
    """按《prompt 契约》§六 判定；`gated=False` 的条目只记录、不参与 verdict。"""
    gates = [
        _gate("幻觉", "0 次", baseline["hallucinations"], candidate["hallucinations"],
              candidate["hallucinations"] == 0),
        _gate("事实准确率", f"≥ {FACT_ACCURACY_MIN:.0%} 且不低于基线",
              _pct(baseline["fact_accuracy"]), _pct(candidate["fact_accuracy"]),
              _ge(candidate["fact_accuracy"], FACT_ACCURACY_MIN)
              and _not_lower(candidate["fact_accuracy"], baseline["fact_accuracy"])),
        _gate("输出结构", "全部通过", _pct(baseline["structure_pass_rate"]),
              _pct(candidate["structure_pass_rate"]),
              candidate["structure_pass_rate"] == 1.0),
        _gate("Tool 选择", "100%（本回合可判定子集）", _pct(baseline["tool_pass_rate"]),
              _pct(candidate["tool_pass_rate"]),
              candidate["tool_pass_rate"] == 1.0,
              note="本回合只判可观测子集，N/A 清单见「已知偏差」"),
        _gate("任务完成率", "Top-5 ≥ 80%、首选 ≥ 50%，且都不下降",
              f"Top-5 {_pct(baseline['top5_hit_rate'])} / 首选 {_pct(baseline['first_hit_rate'])}",
              f"Top-5 {_pct(candidate['top5_hit_rate'])} / 首选 {_pct(candidate['first_hit_rate'])}",
              _ge(candidate["top5_hit_rate"], TOP5_MIN)
              and _ge(candidate["first_hit_rate"], FIRST_MIN)
              and _not_lower(candidate["top5_hit_rate"], baseline["top5_hit_rate"])
              and _not_lower(candidate["first_hit_rate"], baseline["first_hit_rate"])),
        _gate("成本", f"≤ {COST_MAX_CNY} 元/热点", _cny(baseline["mean_cost_cny"]),
              _cny(candidate["mean_cost_cny"]),
              _le(candidate["mean_cost_cny"], COST_MAX_CNY)),
        _gate("速度", f"≤ {LATENCY_MAX_MS / 1000:.0f} 秒/热点",
              _secs(baseline["mean_latency_ms"]), _secs(candidate["mean_latency_ms"]),
              _le(candidate["mean_latency_ms"], LATENCY_MAX_MS)),
    ]
    gates.append(_gate("文案可用率", "仅记录（人工 1–5 分），不进门槛",
                       "—", "待人工（1–5 分）", True, gated=False,
                       note="契约 §六：文字产出没有自动判定口径，不作为回退依据"))
    return gates


def verdict(gates: list[dict]) -> str:
    """任一**进门槛**的维度不达标即回退。"""
    return "回退" if any(not gate["passed"] for gate in gates if gate["gated"]) else "准入"


def _ge(value: float | None, minimum: float) -> bool:
    return value is not None and value >= minimum


def _le(value: float | None, maximum: float) -> bool:
    return value is not None and value <= maximum


def _not_lower(candidate: float | None, baseline: float | None) -> bool:
    if candidate is None or baseline is None:
        return True
    return candidate >= baseline - 1e-9


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _cny(value: float | None) -> str:
    return "—" if value is None else f"{value:.6f} 元"


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value / 1000:.1f} 秒"


def report_path(task: str, version: int, date: str, out_dir: Path = REPORTS_DIR) -> Path:
    """契约 §六 的归档名：`prompt-<task_id>-v<N>-<日期>.md`。"""
    return Path(out_dir) / f"prompt-{task}-v{version}-{date}.md"


# ---------------------------------------------------------------- 报告渲染


def render_markdown(report: dict) -> str:
    """把报告 dict 渲染成给人看的 Markdown。"""
    candidate = report["arms"]["candidate_metrics"]
    baseline = report["arms"]["baseline_metrics"]
    same_prompt = report["prompt"]["same_as_baseline"]
    lines: list[str] = [
        f"# Prompt 变更回归报告｜{report['task']} v{report['prompt']['baseline_version']}"
        f" → v{report['prompt']['candidate_version']}",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 用例：{report['cases_run']} / {report['cases_total']} 组"
        f"（evals {report['eval_version']}，冻结于 {report['manifest_frozen_at']}）",
        f"- 模型：{report['environment']['llm']}；临时库：{report['environment']['postgres_image']}",
        f"- 结论：**{report['verdict']}**"
        + ("（两臂 prompt 正文相同，本次只验证脚本链路）" if same_prompt else ""),
    ]
    scope = (f"`{report['prompt']['baseline_ref']}` 的旧正文 vs 工作区当前正文"
             f"（只跑了前 {report['limit']} 组用例）" if report["limit"]
             else f"`{report['prompt']['baseline_ref']}` 的旧正文 vs 工作区当前正文")
    lines.insert(3, f"- 两臂：{scope}")
    if report["note"]:
        lines.append(f"- 本次改动理由（`--note`）：{report['note']}")
    if same_prompt:
        lines += ["", "> ⚠️ **两臂 prompt 正文相同**：`git diff` 为空，本报告的对比数字只用于"
                      "验证脚本链路，不能当作变更评估。"]

    lines += [
        "",
        "## 一、口径说明",
        "",
        "- **两臂**：同一份用例、同一份示例素材库、同一套模型参数；只有被改的那个 prompt 的正文"
        "不同（基线臂在工具层用旧正文）。",
        "- **事实准确率**＝`must` 要素命中数 ÷ `must` 总数（跨用例汇总）；要素命中按"
        "「类型一致 + 取值归一化后相等或互相包含」判（标注写「健身」、产出「健身房撸铁」算命中）。"
        "`allowed` 覆盖与「产出要素可回溯率」作为观察项列出，不进门槛；产出为空由「输出结构」兜住。",
        "- **幻觉**＝命中 `clue_expectations.forbidden` 次数 + 召回 `must_not` 素材次数；"
        f"人工核对项（{HALLUCINATION_GUARDS_NOTE}）。",
        "- **Tool 选择**只判本回合可观测的子集：`recall_both_channels`（有候选的用例，"
        "`recall_sources` 必须同时含 literal 与 vector）、`no_candidate_padding`（全缺口用例"
        "候选数必须为 0）；其余期望标 N/A 并写明原因。",
        "- **任务完成率**的正确答案＝`must_hit ∪ acceptable`；全缺口用例不进 Top-5 / 首选的分母"
        f"（本次分母 {candidate['scored_cases']} 组），与 S2.8 同口径。",
        "- **成本**＝该用例的 LLM 成本（`runs.totals`）+ 查询向量成本；**速度**＝该用例端到端耗时。"
        "两者都取均值进门槛，报告同时给最大值。",
        "- **文案可用率**：人工 1–5 分，只记录、不进门槛（契约 §六）。",
        "",
        "## 二、门槛判定",
        "",
        "| 维度 | 门槛 | 基线臂 | 候选臂 | 结论 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in report["gates"]:
        mark = "—" if not gate["gated"] else ("✅ 达标" if gate["passed"] else "❌ 不达标")
        note = f"（{gate['note']}）" if gate["note"] else ""
        lines.append(f"| {gate['dimension']} | {gate['threshold']} | {gate['baseline']} | "
                     f"{gate['candidate']} | {mark}{note} |")
    lines += ["", f"**结论：{report['verdict']}**（任一进门槛的维度不达标即回退）", ""]

    lines += [
        "## 三、主表（两臂对照）",
        "",
        "| 指标 | 基线臂（旧 prompt） | 候选臂（新 prompt） | 目标 | 变化 |",
        "| --- | --- | --- | --- | --- |",
        f"| Top-5 命中率 | {_pct(baseline['top5_hit_rate'])} | {_pct(candidate['top5_hit_rate'])} | "
        f"≥ 80% | {_delta(candidate['top5_hit_rate'], baseline['top5_hit_rate'])} |",
        f"| 首选命中率 | {_pct(baseline['first_hit_rate'])} | {_pct(candidate['first_hit_rate'])} | "
        f"≥ 50% | {_delta(candidate['first_hit_rate'], baseline['first_hit_rate'])} |",
        f"| 事实准确率（must 命中） | {_pct(baseline['fact_accuracy'])} | "
        f"{_pct(candidate['fact_accuracy'])} | ≥ 90% | "
        f"{_delta(candidate['fact_accuracy'], baseline['fact_accuracy'])} |",
        f"| ↳ allowed 覆盖（观察项） | {_pct(baseline['expected_coverage'])} | "
        f"{_pct(candidate['expected_coverage'])} | — | "
        f"{_delta(candidate['expected_coverage'], baseline['expected_coverage'])} |",
        f"| ↳ 产出要素可回溯率（观察项） | {_pct(baseline['grounded_precision'])} | "
        f"{_pct(candidate['grounded_precision'])} | — | "
        f"{_delta(candidate['grounded_precision'], baseline['grounded_precision'])} |",
        f"| 幻觉 | {baseline['hallucinations']} 次 | {candidate['hallucinations']} 次 | 0 次 | "
        f"{candidate['hallucinations'] - baseline['hallucinations']:+d} |",
        f"| 输出结构通过率 | {_pct(baseline['structure_pass_rate'])} | "
        f"{_pct(candidate['structure_pass_rate'])} | 100% | — |",
        f"| Tool 选择通过率 | {_pct(baseline['tool_pass_rate'])} | {_pct(candidate['tool_pass_rate'])} | "
        "100% | — |",
        f"| 线索覆盖度 | {_pct(baseline['mean_coverage'])} | {_pct(candidate['mean_coverage'])} | "
        f"（观察项） | {_delta(candidate['mean_coverage'], baseline['mean_coverage'])} |",
        f"| 单次耗时（均值 / 最大） | {_secs(baseline['mean_latency_ms'])} / "
        f"{_secs(baseline['max_latency_ms'])} | {_secs(candidate['mean_latency_ms'])} / "
        f"{_secs(candidate['max_latency_ms'])} | ≤ 60 秒 | — |",
        f"| 单次成本（均值 / 最大） | {_cny(baseline['mean_cost_cny'])} / "
        f"{_cny(baseline['max_cost_cny'])} | {_cny(candidate['mean_cost_cny'])} / "
        f"{_cny(candidate['max_cost_cny'])} | ≤ 0.05 元 | — |",
        "| 文案可用率 | 待人工（1–5 分） | 待人工（1–5 分） | 仅记录 | — |",
        "",
        "## 四、逐用例变化",
        "",
        "| 用例 | must 命中 | allowed 覆盖 | 产出可回溯率 | Top-5 | 首选 | 覆盖度 | 耗时 | 成本 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case_id in sorted(report["cases"]):
        old = report["cases"][case_id]["baseline"]
        new = report["cases"][case_id]["candidate"]
        notes = "；".join(new.get("structure_problems") or []) or ""
        if new.get("tool_problems"):
            notes = "；".join([notes, *new["tool_problems"]]).strip("；")
        if new.get("error"):
            notes = f"运行失败：{new['error'][:80]}"
        must_marks = f"{old['must_matched']}/{old['must_total']} → " \
                     f"{new['must_matched']}/{new['must_total']}"
        expected_marks = f"{old['expected_matched']}/{old['expected_total']} → " \
                         f"{new['expected_matched']}/{new['expected_total']}"
        precision = f"{_pct(old.get('grounded_precision'))} → " \
                    f"{_pct(new.get('grounded_precision'))}"
        lines.append(
            f"| {case_id} | {must_marks} | {expected_marks} | {precision} | "
            f"{_mark(new.get('top5_hit'))} | {_mark(new.get('first_hit'))} | "
            f"{_pct(old.get('coverage_ratio'))} → {_pct(new.get('coverage_ratio'))} | "
            f"{_secs(new.get('latency_ms'))} | {_cny(new.get('cost_cny'))} | {notes} |")

    diff = report["prompt"]["diff"]
    lines += [
        "",
        "## 五、prompt 版本与改动",
        "",
        f"- 基线臂使用版本：v{report['prompt']['baseline_version']}"
        f"（来自 `{report['prompt']['baseline_ref']}`）",
        f"- 候选臂使用版本：v{report['prompt']['candidate_version']}",
        f"- 两臂实际落库版本（`RunResult.prompt_versions` 汇总）："
        f"基线 {json.dumps(report['arms']['baseline_versions'], ensure_ascii=False)}；"
        f"候选 {json.dumps(report['arms']['candidate_versions'], ensure_ascii=False)}",
        "",
        "```diff",
        diff or "（两臂 prompt 正文相同，没有 diff）",
        "```",
        "",
        "## 六、已知偏差",
        "",
        "- 用例集与示例素材包都是脱敏合成数据，绝对数字不能外推到真实素材库；这里看的是**两臂之间的变化**。",
        "- 本次只改了一个 prompt；其余两个任务的正文在两臂里相同。",
        f"- {HALLUCINATION_GUARDS_NOTE}；"
        f"Tool 期望的 N/A 清单见第二节与本表下方。",
    ]
    for name, reason in TOOL_EXPECTATIONS_NA.items():
        lines.append(f"- `{name}` 标 N/A：{reason}")
    return "\n".join(line for line in lines if line is not None) + "\n"


def _mark(flag: bool | None) -> str:
    if flag is None:
        return "—"
    return "✅" if flag else "❌"


def _delta(candidate: float | None, baseline: float | None) -> str:
    if candidate is None or baseline is None:
        return "—"
    return f"{(candidate - baseline) * 100:+.1f} pp"


def write_report(report: dict, *, out_dir: Path) -> tuple[Path, Path]:
    """落盘 md + json（契约 §六 的命名规则）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    date = report["generated_at"][:10]
    md_path = report_path(report["task"], report["prompt"]["candidate_version"], date, out_dir)
    json_path = md_path.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return md_path, json_path


# ---------------------------------------------------------------- 运行期


class EvalCfg:
    """只覆盖素材 / 索引 / 运行产物目录的 cfg 包装，其余字段透传真实 `AppConfig`。"""

    def __init__(self, base: AppConfig, materials_dir: str, index_dir: str,
                 runs_dir: str) -> None:
        self._base = base
        self._materials_dir = materials_dir
        self._index_dir = index_dir
        self._runs_dir = runs_dir

    def materials_dir(self) -> str:
        return self._materials_dir

    def index_dir(self) -> str:
        return self._index_dir

    def runs_dir(self) -> str:
        return self._runs_dir

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


class RecordingEmbedder:
    """查询向量缓存 + token 记账（两臂共用，避免同一句话重复调接口）。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.cache: dict[str, list[float]] = {}
        self.prompt_tokens = 0
        self.calls = 0

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        for content in texts:
            if content not in self.cache:
                result = await self.inner.embed([content])
                self.cache[content] = list(result.vectors[0])
                self.prompt_tokens += result.prompt_tokens
                self.calls += 1
            vectors.append(list(self.cache[content]))
        return EmbeddingResult(vectors=vectors, model=getattr(self.inner, "model", ""),
                               prompt_tokens=0, attempts=1)


def _require_docker() -> None:
    try:
        docker.from_env().ping()
    except Exception as exc:
        raise EvalError("评测需要可用的 Docker：请先启动 Docker Desktop"
                        f"（原始错误：{type(exc).__name__}: {exc}）") from exc


def _init_statements() -> list[str]:
    body = "\n".join(line for line in
                     (INIT_DIR / "01-extensions.sql").read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("--"))
    return [statement.strip() for statement in body.split(";") if statement.strip()]


async def prepare_schema(engine: AsyncEngine) -> None:
    """建扩展 + 建表（与 S2.8 同口径：临时库用 create_all 快速搭台）。"""
    async with engine.begin() as conn:
        for statement in _init_statements():
            await conn.execute(text(statement))
        names = set((await conn.execute(text("SELECT extname FROM pg_extension"))).scalars())
        missing = {"vector", "pg_trgm"} - names
        if missing:
            raise EvalError(f"评测库缺少扩展：{sorted(missing)}（检查 {INIT_DIR}）")
        await conn.run_sync(Base.metadata.create_all)


async def seed_library(session: Any, eval_cfg: EvalCfg, embedder: Any) -> None:
    """只装入 demo_pack：先建索引，再用真实客户端回填向量。"""
    await sync_materials(session, eval_cfg, vision=None)
    await backfill_embeddings(session, eval_cfg, embedder=embedder)


async def run_arm(session: Any, eval_cfg: EvalCfg, cases: list[dict], *, task: str,
                  baseline_text: str | None, recording: RecordingEmbedder,
                  now: float, price: float) -> tuple[list[dict], set[int]]:
    """跑一条臂：逐用例走完整五节点链路（真模型），返回逐用例结果与实际用的 prompt 版本。"""
    results: list[dict] = []
    versions: set[int] = set()
    with baseline_prompt(task, baseline_text):
        for case in cases:
            tokens_before = recording.prompt_tokens
            started = time.perf_counter()
            clue: dict = {}
            candidates: list[dict] = []
            draft: dict | None = None
            coverage_ratio: float | None = None
            cost = 0.0
            try:
                run = await run_analysis([case["hotspot"]], cfg=eval_cfg, session=session,
                                         embedder=recording, now=now)
                entry = (run.hotspots or [{}])[0]
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                error = str(entry.get("error") or "")
                clue = entry.get("clue") or {}
                candidates = entry.get("candidates") or []
                draft = entry.get("draft")
                coverage_ratio = (entry.get("coverage") or {}).get("ratio")
                embedding_cost = ((recording.prompt_tokens - tokens_before) / 1_000_000
                                  * price)
                cost = round(float(run.totals.get("cost_cny") or 0.0) + embedding_cost, 8)
                for version in (run.prompt_versions or {}).values():
                    versions.add(int(version))
            except Exception as exc:      # 单用例失败不影响整臂，但如实记账
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                error = f"{type(exc).__name__}: {exc}"
            scored = score_case_dimensions(case, clue=clue, candidates=candidates,
                                           draft=draft, error=error)
            scored["latency_ms"] = latency_ms
            scored["cost_cny"] = cost
            scored["coverage_ratio"] = coverage_ratio if not error else None
            results.append(scored)
    return results, versions


async def evaluate(cfg: AppConfig, args: argparse.Namespace) -> dict:
    """完整流程：临时库 → 装 demo_pack → 两臂跑 20 组用例 → 聚合 → 门槛判定。"""
    task = args.task
    candidate_text = candidate_prompt_text(task)
    candidate_version = read_prompt_version(candidate_text)
    baseline_text = load_baseline_prompt(args.baseline, task)
    baseline_version = read_prompt_version(baseline_text)
    diff = prompt_diff(args.baseline, task)

    all_cases = load_cases()
    cases = all_cases
    if args.limit:
        cases = cases[: max(1, int(args.limit))]
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    _require_docker()
    with tempfile.TemporaryDirectory(prefix="xhs-prompt-eval-") as tmp:
        eval_cfg = EvalCfg(cfg, materials_dir=str(PACK_DIR),
                           index_dir=str(Path(tmp) / "index"),
                           runs_dir=str(Path(tmp) / "runs"))
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
                async with build_http_client(cfg.embedding.timeout_s) as http:
                    real = build_embedder(cfg, client=http)
                    if real is None:
                        raise ConfigError(
                            "向量召回未启用（[embedding].enabled = false）",
                            "评测需要向量通道：把 [embedding].enabled 设为 true")
                    recording = RecordingEmbedder(real)
                    async with factory() as session:
                        await seed_library(session, eval_cfg, recording)
                        now = time.time()
                        price = cfg.embedding.price_in_per_m
                        baseline_results, baseline_versions = await run_arm(
                            session, eval_cfg, cases, task=task, baseline_text=baseline_text,
                            recording=recording, now=now, price=price)
                        candidate_results, candidate_versions = await run_arm(
                            session, eval_cfg, cases, task=task, baseline_text=None,
                            recording=recording, now=now, price=price)
            finally:
                await engine.dispose()
        finally:
            if not args.keep_container:
                container.stop()

    baseline_metrics = aggregate_dimensions(baseline_results)
    candidate_metrics = aggregate_dimensions(candidate_results)
    gates = evaluate_gates(baseline_metrics, candidate_metrics)
    by_case: dict[str, dict] = {}
    for old, new in zip(baseline_results, candidate_results, strict=True):
        by_case[new["case"]] = {"baseline": old, "candidate": new}

    return {
        "task": task,
        "generated_at": dt.datetime.now(dt.UTC).astimezone().isoformat(timespec="seconds"),
        "eval_version": manifest.get("version"),
        "manifest_frozen_at": manifest.get("frozen_at"),
        "cases_run": len(cases),
        "cases_total": len(all_cases),
        "limit": args.limit,
        "note": args.note,
        "verdict": verdict(gates),
        "gates": gates,
        "prompt": {
            "baseline_ref": args.baseline,
            "baseline_version": baseline_version,
            "candidate_version": candidate_version,
            "same_as_baseline": not diff,
            "diff": diff,
        },
        "arms": {
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "baseline_versions": sorted(baseline_versions),
            "candidate_versions": sorted(candidate_versions),
        },
        "cases": by_case,
        "environment": {
            "postgres_image": POSTGRES_IMAGE,
            "python": platform.python_version(),
            "llm": f"{cfg.llm.provider} / {cfg.llm.model}",
            "embedding_model": cfg.embedding.model,
            "embedding_price_in_per_m": cfg.embedding.price_in_per_m,
        },
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prompt 变更回归报告（prompt 契约 §六）")
    parser.add_argument("--task", required=True, choices=list(TASKS),
                        help="本次改动的是哪个 prompt（决定报告归档名）")
    parser.add_argument("--baseline", default="HEAD",
                        help="基线臂旧正文的 git ref（默认 HEAD）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 组用例（冒烟用）")
    parser.add_argument("--out", default=str(REPORTS_DIR), help="报告输出目录")
    parser.add_argument("--note", default="", help="本次为什么改（写进报告，契约要求）")
    parser.add_argument("--keep-container", action="store_true", help="跑完保留容器（调试用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cfg = load_config()
        if not cfg.embedding.enabled:
            raise ConfigError("向量召回未启用（[embedding].enabled = false）",
                              "评测需要向量通道：把 config/config.toml 的 [embedding].enabled 设为 true")
        if not cfg.llm.resolved_key():
            raise ConfigError(f"文本模型密钥为空：{cfg.llm.api_key_env}",
                              "把密钥写进 config/.env 或导出同名环境变量")
        report = asyncio.run(evaluate(cfg, args))
    except (ConfigError, EmbeddingError, EvalError) as exc:
        print(f"{getattr(exc, 'code', 'E_EVAL')}: {getattr(exc, 'message', None) or exc}",
              file=sys.stderr)
        fix = getattr(exc, "fix", "")
        if fix:
            print(f"修复：{fix}", file=sys.stderr)
        return 1
    except Exception as exc:      # Docker / 网络 / 数据库：直接失败，不降级
        print(f"E_EVAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    md_path, json_path = write_report(report, out_dir=Path(args.out))
    candidate = report["arms"]["candidate_metrics"]
    print(f"回归报告：{md_path}")
    print(f"原始数字：{json_path}")
    print(f"结论：{report['verdict']}")
    for gate in report["gates"]:
        if not gate["gated"]:
            continue
        print(f"  {'✅' if gate['passed'] else '❌'} {gate['dimension']}："
              f"基线 {gate['baseline']} → 候选 {gate['candidate']}（{gate['threshold']}）")
    print(f"用例 {candidate['cases']} 组；耗时均值 {candidate['mean_latency_ms']} ms；"
          f"成本均值 {candidate['mean_cost_cny']} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

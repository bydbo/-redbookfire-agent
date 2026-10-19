"""冻结 E7 上下文评测的离线入口与限额组件实测。

默认只验证冻结资产。--live 才发送模型请求；所有实测共享 --ledger 的持久预算，
每次传输前预留上界，缺 usage / 异常保留预留，不在重启后重置。不得把组件实测当全量验收。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from context_eval_budget import BudgetExceeded, BudgetTransport, SpendLedger

from xhs_agent.agents.revise import revise_draft
from xhs_agent.config import load_config
from xhs_agent.schemas import ContextSnapshot, HotspotClue
from xhs_agent.services.context_state import StoredContext, extract
from xhs_agent.services.image_clue import parse_image_clue, parse_image_clue_multi
from xhs_agent.tools.llm import (
    LLMCall,
    StructuredCaller,
    build_provider,
)

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals/context-repair-v2"


def validate_assets() -> dict[str, Any]:
    manifest = json.loads((SUITE / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if hashlib.sha256((SUITE / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"冻结资产校验失败：{name}")
    suite = json.loads((SUITE / "cases.json").read_text(encoding="utf-8"))
    if len(suite["cases"]) != 12 or sum(len(c["turns"]) for c in suite["cases"]) * 6 != 648:
        raise ValueError("冻结样例数量 / 轮次不匹配")
    return suite


async def live(args: argparse.Namespace, suite: dict[str, Any]) -> int:
    cfg = load_config()
    cfg.llm.max_retries = 0
    cfg.llm.max_tokens = 1800
    # 只对已核验的实际官方文本端点启用；不改变用户配置文件。
    if cfg.llm.base_url.rstrip("/") in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        cfg.llm.prefix_usage_supported = True
        cfg.llm.thinking_toggle_supported = True
    cfg.context.extract_max_tokens = 1800
    ledger = SpendLedger(Path(args.ledger), args.budget_cny)
    output = Path(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    previous = output / "model-results.json"
    earlier = json.loads(previous.read_text(encoding="utf-8")) if previous.exists() else {}
    report: dict[str, Any] = {"kind": "component_live", "suite": suite["version"],
        "started_at": datetime.now(UTC).isoformat(), "results": [], "human_scores": "pending",
        "full_72_sessions_648_turns": "not_executed", "model": cfg.llm.model,
        "code_ref": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "worktree_dirty": bool(subprocess.check_output(["git", "diff", "--name-only"], cwd=ROOT, text=True,
                                                        stderr=subprocess.DEVNULL).strip())}
    report["results"] = list(earlier.get("results") or [])
    report["runs"] = [*list(earlier.get("runs") or []), {"phase": args.phase,
                       "started_at": report["started_at"], "code_ref": report["code_ref"]}]
    report["execution_errors"] = list(earlier.get("execution_errors") or [])
    if earlier.get("environment_error"):
        report["execution_errors"].append({"phase": (earlier.get("runs") or [{}])[-1].get("phase"),
                                           "error": earlier["environment_error"]})
    transport = BudgetTransport(ledger, cfg)
    initial_results = len(report["results"])

    def checkpoint() -> None:
        report["budget_upper_cny"] = round(ledger.committed, 6)
        report["http_records"] = [*list(earlier.get("http_records") or []), *transport.last]
        (output / "model-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        async with httpx.AsyncClient(transport=transport, timeout=cfg.llm.timeout_s) as client:
            provider = build_provider(cfg, client=client)
            if args.phase in ("all", "extract"):
                for case in suite["cases"][:4]:
                    for index in (-3, -2):
                        text = case["turns"][index]
                        caller = StructuredCaller(provider, max_repairs=1)
                        stored = StoredContext(session_id=uuid.uuid4(), state_version=0, topic_id="t1",
                                               snapshot=ContextSnapshot(topic_id="t1"))
                        try:
                            delta, result = await extract(cfg, caller, user_text=text, stored=stored)
                            row = {"id": case["id"], "kind": "extract", "input": text,
                                   "delta": delta.model_dump(), "raw_output": result.text,
                                   "records": caller.records,
                                   "status": "succeeded"}
                        except BudgetExceeded:
                            raise
                        except Exception as exc:
                            row = {"id": case["id"], "kind": "extract", "input": text,
                                   "status": "failed", "error": type(exc).__name__, "records": caller.records}
                        report["results"].append(row)
                        checkpoint()
            if args.phase in ("all", "revise"):
                case = next(c for c in suite["cases"] if c["id"] == "T05")
                clue = HotspotClue.from_dict({"hotspot_raw": "夜跑", "why_it_works": ["低门槛"],
                    "mechanisms": [{"name": "可执行"}], "elements": [{"type": "topic", "value": "夜跑",
                    "weight": 1, "confidence": 1, "evidence": "夜跑"}]})
                draft = {"titles": [{"text": "慢慢开始夜跑", "style": "建议"}], "body": case["fixture"]["drafts"][0],
                         "tags": ["夜跑"], "cover_text": "从慢开始", "first_3s": "走出门", "shot_list": [], "compliance_notes": []}
                for text in case["turns"][1:4]:
                    caller = StructuredCaller(provider, max_repairs=1)
                    base = dict(draft)
                    try:
                        outcome = await revise_draft(clue, None, draft, text, caller,
                            constraints=[{"kind": "constraint", "value": "不要使用感叹号"}])
                        result = outcome.value
                        if result.draft is not None:
                            draft = result.draft.to_dict()
                        row = {"id": "T05", "kind": "revise", "input": text, "base": base,
                               "result": draft, "refused": result.refused, "records": caller.records, "status": "succeeded"}
                    except BudgetExceeded:
                        raise
                    except Exception as exc:
                        row = {"id": "T05", "kind": "revise", "status": "failed", "error": type(exc).__name__, "records": caller.records}
                    report["results"].append(row)
                    checkpoint()
            if args.phase in ("all", "prefix"):
                prefix = "\n".join(f"素材{i:03}：夜跑、公园、树影、轻松开始。" for i in range(120))
                for group in range(args.prefix_groups):
                    for request_no in range(3):
                        result = await provider.complete(LLMCall(task="context_prefix_probe", system="只回答收到。",
                            user=f"{prefix}\n核对组{group}，请求{request_no}。", json_mode=False, max_tokens=32))
                        report["results"].append({"kind": "prefix", "group": group, "request": request_no,
                            "status": "failed" if result.error else "succeeded", "record": result.record("context_prefix_probe")})
                        checkpoint()
            if args.phase in ("all", "vision"):
                paths = [output / "night-run.png", output / "camping.png"]
                if not all(path.exists() for path in paths):
                    raise ValueError("先用浏览器脚本把冻结 SVG 栅格化到输出目录")
                cfg.vision.enabled = True
                for images in ([paths[0]], paths):
                    row = {"kind": "vision", "images": [path.name for path in images],
                        "sha256": [hashlib.sha256(path.read_bytes()).hexdigest() for path in images],
                        "prompt_versions": {}}
                    try:
                        parsed = (await parse_image_clue(cfg, images[0].read_bytes(), "image/png", http=client)
                                  if len(images) == 1 else await parse_image_clue_multi(cfg,
                                      [(path.read_bytes(), "image/png") for path in images], http=client))
                        row.update(status="succeeded", raw_text=parsed.raw_text, clue=parsed.clue.to_dict(),
                            prompt_versions=parsed.prompt_versions, prompt_tokens=parsed.prompt_tokens,
                            completion_tokens=parsed.completion_tokens, cost_cny=parsed.cost_cny,
                            usage_status=parsed.usage_status)
                    except BudgetExceeded:
                        raise
                    except Exception as exc:
                        detail = getattr(exc, "detail", {})
                        row.update(status="failed", error=type(exc).__name__,
                                   error_reason=str(exc)[:1000],
                                   usage=detail.get("usage"), prompt_versions=detail.get("prompt_versions", {}))
                    report["results"].append(row)
                    checkpoint()
    except BudgetExceeded as exc:
        report["stopped"] = str(exc)
        checkpoint()
        return 1
    except Exception as exc:
        report["environment_error"] = type(exc).__name__
        checkpoint()
        raise
    finally:
        checkpoint()
        ledger.close()
    rows = [{"id": str(i), "kind": row["kind"], "constraint_score": "", "task_score": "", "notes": ""}
            for i, row in enumerate(report["results"]) if row["kind"] != "prefix"]
    (output / "human-score-template.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output / "model-results.json"), "budget_upper_cny": ledger.committed,
                      "human_scores": "pending"}, ensure_ascii=False))
    return 0 if all(row["status"] == "succeeded" for row in report["results"][initial_results:]) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--phase", choices=("all", "extract", "revise", "prefix", "vision"), default="all")
    parser.add_argument("--prefix-groups", type=int, default=30)
    parser.add_argument("--budget-cny", type=float, default=10)
    parser.add_argument("--ledger", default="runs/_e7b6_final/spend-ledger.json")
    parser.add_argument("--out-dir", default="runs/_e7b6_final")
    parser.add_argument("--offline-tests", action="store_true")
    args = parser.parse_args()
    suite = validate_assets()
    if not args.live:
        print("冻结资产校验通过；12 用例，72 会话 / 648 轮完整计划；本次不调用模型。")
        if args.offline_tests:
            return subprocess.call([sys.executable, "-m", "pytest", "-q", "tests/unit/test_context_repair_regressions.py",
                "tests/unit/test_context_recovery_candidates.py", "tests/unit/test_context_metrics.py", "tests/unit/test_model_attempts.py"], cwd=ROOT)
        return 0
    return asyncio.run(live(args, suite))


if __name__ == "__main__":
    raise SystemExit(main())

"""冻结用例的真实会话 API 运行器；临时 Postgres / Redis，默认固定模型，--live 才付费。

每次执行选择一臂 / 重复次数；结果可合并核对 72 会话 / 648 轮。旧代码须显式指定
--source-root 并记录实际 Git ref；不把候选代码 off 模式伪装成旧代码基线。
固定模型仅为验证传输、状态及操作闭环，不能充当真实模型效果证据或人工评分。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from context_eval_budget import BudgetExceeded, BudgetTransport, SpendLedger

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals/context-repair-v2"
PNG = bytes.fromhex("89504e470d0a1a0a") + b"fixture"


def source_ref(source: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()


def code_fingerprint(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((source / "src").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".md", ".json"):
            digest.update(path.relative_to(source).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def execution_failures(report: dict[str, Any]) -> list[dict[str, Any]]:
    """只接纳具备完整停止证据的预期拒绝；失败原行仍在结果和分母中。"""
    unexpected = []
    for record in report["sessions"]:
        foreign = next((item["run_id"] for item in record.get("fixture_objects", [])
                        if item.get("owner_session") != record["session_id"] and item.get("owner_session")), None)
        for turn in record["turns"]:
            error = turn.get("http_error") or {}
            expected_rejection = record["case_id"] == "T08" and turn["number"] == 4 \
                and turn.get("http_status") == 404 and turn.get("new_transport_requests") == 0 \
                and error.get("code") == "not_found" and foreign is not None \
                and (error.get("detail") or {}).get("run_id") == foreign
            expected_overflow = record["case_id"] == "T09" and turn["number"] >= 4 \
                and turn.get("status") == "failed" and turn.get("new_transport_requests") == 0 \
                and len((turn.get("context") or {}).get("facts", [])) == 41 \
                and turn.get("error_category") == "required_context_overflow"
            if expected_rejection or expected_overflow:
                turn["expected_stop_verified"] = True
            elif record["case_id"] == "T08" and turn["number"] == 4 \
                    or record["case_id"] == "T09" and turn["number"] >= 4:
                unexpected.append({"case_id": record["case_id"], "turn": turn["number"], "reason": "expected_stop_missing"})
            elif turn.get("status") == "failed" or turn.get("http_status", 200) >= 400:
                unexpected.append({"case_id": record["case_id"], "turn": turn["number"]})
        if any(probe.get("passed") is False for probe in record["protocol_probes"]):
            unexpected.append({"case_id": record["case_id"], "reason": "protocol_probe_failed"})
    return unexpected


def stream_error_category(response: httpx.Response) -> str:
    """准备阶段尚未产生进度快照时，失败类别来自真实 SSE error 信封。"""
    for event in response.text.replace("\r\n", "\n").split("\n\n"):
        if "event: error" not in event.splitlines():
            continue
        for line in event.splitlines():
            if line.startswith("data:"):
                try:
                    return str(json.loads(line[5:]).get("error_category") or "")
                except (ValueError, AttributeError):
                    return ""
    return ""


def validate_suite() -> dict[str, Any]:
    manifest = json.loads((SUITE / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        if hashlib.sha256((SUITE / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"冻结资产已改变：{name}")
    return json.loads((SUITE / "cases.json").read_text(encoding="utf-8"))


def fixture_draft(body: str) -> dict[str, Any]:
    return {"titles": [{"text": "慢慢开始", "style": "建议"}], "body": body, "tags": ["夜跑"],
            "cover_text": "开始", "first_3s": "走出门", "shot_list": [], "compliance_notes": []}


def fixture_clue() -> dict[str, Any]:
    return {"hotspot_raw": "夜跑", "why_it_works": ["可执行"], "mechanisms": [{"name": "行动", "explain": "慢慢开始"}],
        "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9, "confidence": 0.9, "evidence": "夜跑"},
                     {"type": "scene", "value": "公园", "weight": 0.4, "confidence": 0.9, "evidence": "图1：公园"},
                     {"type": "visual", "value": "路灯", "weight": 0.4, "confidence": 0.9, "evidence": "图2：路灯"}],
        "match_keywords": ["夜跑"], "borrow_angles": ["面向新手"], "risk_notes": []}


class FixedModel:
    """只用于离线执行器验证，显式输出固定业务样本，不评价语义成功率。"""

    def __init__(self) -> None:
        self.binding: dict[str, Any] | None = None
        self.sent = False
        self.calls: list[dict[str, Any]] = []
        self.use_fixture = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        vision = any(isinstance(item.get("content"), list) for item in body.get("messages", []))
        self.calls.append({"model": body.get("model"), "stream": bool(body.get("stream")), "vision": vision})
        joined = str(body.get("messages") or [])
        tools = body.get("tools") or []
        tool_calls = []
        if tools and self.binding and not self.sent:
            self.sent = True
            operation = self.binding["operation"]
            name = "revise_draft" if operation == "revise_draft" else "parse_image_clue"
            args = {"request": "保持简短"} if name == "revise_draft" else {}
            tool_calls = [{"id": "call-" + uuid.uuid4().hex, "type": "function",
                           "function": {"name": name, "arguments": json.dumps(args)}}]
            text = ""
        elif tools:
            text = "已记录当前要求。"
        elif "对话状态记录员" in joined or "对话状态恢复员" in joined:
            text = '{"operations":[],"proposals":[]}'
        elif vision:
            text = json.dumps({"raw_text": "夜跑", "clue": fixture_clue()}, ensure_ascii=False)
        else:
            text = json.dumps({"refused": False, "draft": fixture_draft("固定模型改写结果")}, ensure_ascii=False)
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40}
        message = {"content": text, "tool_calls": tool_calls}
        if body.get("stream"):
            chunk = {"choices": [{"delta": message, "finish_reason": "tool_calls" if tool_calls else "stop"}], "usage": usage}
            return httpx.Response(200, content=("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode(),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": usage})


@contextmanager
def runtime_clients(cfg: Any, *, live: bool, ledger: SpendLedger | None, fixed: FixedModel):
    """显式测试装配，只替换模型传输；API、轮次、工具、数据库和 Redis 走真实代码。"""
    from xhs_agent.api.routers import chat as chat_router
    from xhs_agent.services import chat_turns
    from xhs_agent.workflows import analysis as analysis_workflow

    transports: list[BudgetTransport] = []

    def client(timeout_s: float = 60, **_kwargs: Any) -> httpx.AsyncClient:
        if live and not fixed.use_fixture:
            transport = BudgetTransport(ledger, cfg)
            transports.append(transport)
        else:
            transport = httpx.MockTransport(fixed.handle)
        return httpx.AsyncClient(transport=transport, timeout=timeout_s)

    with ExitStack() as stack:
        for module in (chat_router, chat_turns, analysis_workflow):
            if hasattr(module, "build_http_client"):
                stack.enter_context(patch.object(module, "build_http_client", client))
        # recover 路由在函数内导入这一入口。
        stack.enter_context(patch("xhs_agent.tools.http.build_http_client", client))
        yield transports


async def budget_probe(cfg: Any, factory: Any, sid: str, *, limit: str) -> dict[str, Any]:
    """通过真实准备入口注入坏 JSON；与真实模型语义评分分开记录。"""
    from xhs_agent.core import budget
    from xhs_agent.services.chat import append_message
    from xhs_agent.services.chat_turns import _prepare_context
    from xhs_agent.tools.llm import BaseProvider, LLMResult

    class BrokenThenValid(BaseProvider):
        calls = 0

        async def complete(self, _call):
            self.calls += 1
            return LLMResult(text="not-json" if self.calls == 1 else '{"operations":[],"proposals":[]}',
                             prompt_tokens=100, completion_tokens=20, cost_cny=0.01,
                             usage_status="actual", model="protocol-fixture")

    provider = BrokenThenValid()
    checks = []
    real_decide = budget.decide

    def measured(**kwargs):
        checks.append(kwargs)
        result = real_decide(**kwargs)
        if limit == "initial_overflow" or limit == "repair_overflow" and len(checks) == 2:
            # 用同一估算器对缩小的真实预算再决策；不是直接伪造 allowed=False。
            kwargs["application_limit"] = 1
            result = real_decide(**kwargs)
        return result

    ledger: dict[str, Any] = {}
    async with factory() as db:
        message = await append_message(db, uuid.UUID(sid), role="user", content="保持简短")
        with patch.object(budget, "decide", measured):
            prep = await _prepare_context(cfg=cfg, session=db, session_id=uuid.UUID(sid),
                user_message_id=uuid.UUID(message["message_id"]), user_text="保持简短", provider=provider,
                turn_id=str(uuid.uuid4()), ledger=ledger)
    expected = {"fits": (2, 2), "repair_overflow": (2, 1), "initial_overflow": (1, 0)}[limit]
    observed = (len(checks), provider.calls)
    return {"kind": "deterministic_production_entry", "fixture": limit,
            "checks": observed[0], "requests": observed[1], "expected": list(expected),
            "known_cost_cny": sum(row.get("cost_cny", 0) for row in ledger.get("records", [])),
            "status": getattr(prep, "status", "legacy_no_admission_status"), "passed": observed == expected}


async def replay_revision(cfg: Any, factory: Any, sid: str, version_id: str, request: str) -> dict[str, Any]:
    """复用原认领的 turn/call/source 调生产工具；重复提交不等于发送一条新消息。"""
    from xhs_agent.core.context_policy import TurnContextPolicy
    from xhs_agent.db.models import ChatMessage
    from xhs_agent.services import artifact_versions, chat_tools
    from xhs_agent.tools.http import build_http_client
    from xhs_agent.tools.llm import StructuredCaller, build_provider

    async with factory() as db, build_http_client() as http:
        row = await artifact_versions.get_version(db, uuid.UUID(sid), uuid.UUID(version_id))
        if row is None or not row.source_message_id:
            raise ValueError("没有可重放的原始改稿认领")
        source = await db.get(ChatMessage, row.source_message_id)
        ctx = chat_tools.ToolContext(cfg=cfg, session=db, session_id=uuid.UUID(sid),
            caller=StructuredCaller(build_provider(cfg, client=http)), policy=TurnContextPolicy.from_config(cfg, sid),
            turn_id=row.source["turn_id"], checkpoint_scope=row.source["tool_call_id"],
            source_message_id=str(source.id), operation_binding=source.operation_binding)
        result = await chat_tools.revise_draft(ctx, {"request": request})
        return {"status": result.status, "payload": result.payload, "error": result.error}


async def run(args: argparse.Namespace, suite: dict[str, Any], postgres: str, redis_url: str) -> int:
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from xhs_agent.api.deps import get_config, get_dispatcher, get_session
    from xhs_agent.api.main import create_app
    from xhs_agent.config import AppConfig, EnvView, load_config
    from xhs_agent.core.tracing import get_tracer, reset_tracer
    from xhs_agent.db import Base
    from xhs_agent.db.models import ChatSession, Hotspot, Run, RunHotspot
    from xhs_agent.services.chat import append_message

    source = Path(args.source_root).resolve()
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    cfg = load_config() if args.live else AppConfig()
    old_env = cfg.env_view
    env = {name: old_env.get(name) for name in (cfg.llm.api_key_env, cfg.vision.api_key_env, "XHS_LLM_API_KEY")}
    env.update(DATABASE_URL=postgres, REDIS_URL=redis_url)
    if not args.live:
        env.update({cfg.llm.api_key_env: "fixture", cfg.vision.api_key_env: "fixture"})
    cfg._env = EnvView({}, env)
    for section in (cfg.llm, cfg.vision, cfg.embedding):
        section._env = cfg._env
    reset_tracer()
    get_tracer(cfg)
    cfg.paths.runs_dir = str(output / "business")
    cfg.frontend.serve = False
    cfg.embedding.enabled = False
    cfg.vision.enabled = True
    if hasattr(cfg, "context"):
        cfg.context.mode = args.arm
        cfg.context.model_context_window = 393216
        cfg.context.model_window_verified = True
        cfg.context.enabled_sessions = []
    cfg.llm.max_retries = 0
    cfg.llm.max_tokens = 700
    cfg.chat.max_tool_rounds = 2
    if args.live and cfg.llm.base_url.rstrip("/") in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
        for field in ("prefix_usage_supported", "stream_usage_supported", "thinking_toggle_supported"):
            if hasattr(cfg.llm, field):
                setattr(cfg.llm, field, True)
    engine = create_async_engine(postgres)
    async with engine.begin() as connection:
        await connection.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(sql_text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(check_startup=False)
    app.dependency_overrides[get_config] = lambda: cfg

    async def database():
        async with factory() as session:
            yield session

    class NoExternalQueue:
        async def enqueue(self, _job: str, **_kwargs: Any) -> None:
            raise RuntimeError("冻结会话评测不允许未计划的分析任务投递")

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_dispatcher] = NoExternalQueue
    ref = source_ref(source)
    dirty = bool(subprocess.check_output(["git", "diff", "--name-only"], cwd=source, text=True, stderr=subprocess.DEVNULL).strip())
    selected = [case for case in suite["cases"] if not args.cases or case["id"] in args.cases.split(",")]
    if not selected:
        raise ValueError("没有选中冻结用例")
    report: dict[str, Any] = {"suite": suite["version"], "actual_ref": ref, "worktree_dirty": dirty,
        "source_sha256": code_fingerprint(source),
        "suite_manifest_sha256": hashlib.sha256((SUITE / "manifest.json").read_bytes()).hexdigest(),
        "model": cfg.llm.model, "vision_model": cfg.vision.model,
        "parameters": {"max_tokens": cfg.llm.max_tokens, "max_tool_rounds": cfg.chat.max_tool_rounds,
                       "extract_max_tokens": cfg.context.extract_max_tokens if hasattr(cfg, "context") else None},
        "arm": args.arm, "kind": "live_session_api" if args.live else "fixed_model_session_api",
        "comparison_kind": "old_baseline" if ref.startswith(suite["baseline_ref"]) and args.arm == "off" else "candidate_or_policy_control",
        "planned_full_sessions": 72, "planned_full_turns": 648, "selected_sessions": len(selected) * args.repeats,
        "human_scoring": "pending" if args.live else "not_applicable_fixed_model", "sessions": [], "started_at": datetime.now(UTC).isoformat()}
    fixed = FixedModel()
    normal_limit = cfg.context.request_token_limit if hasattr(cfg, "context") else None
    normal_output = cfg.llm.max_tokens
    ledger = SpendLedger(Path(args.ledger), args.budget_cny) if args.live else None

    def checkpoint() -> None:
        report["budget_upper_cny"] = ledger.committed if ledger else 0
        target = output / "sessions.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    try:
        with runtime_clients(cfg, live=args.live, ledger=ledger, fixed=fixed) as transports:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture", timeout=240) as client:
                for repeat in range(args.repeats):
                    for case in selected:
                        fixed.use_fixture = case["kind"] in ("budget", "repair_budget")
                        if normal_limit is not None:
                            cfg.context.request_token_limit = normal_limit
                        cfg.llm.max_tokens = normal_output
                        created = (await client.post("/api/chat/sessions")).json()
                        sid = created["session_id"]
                        record = {"case_id": case["id"], "repeat": repeat, "session_id": sid,
                                  "expected": case["expected"], "turns": [],
                                  "model_kind": "protocol_fixture" if fixed.use_fixture else report["kind"],
                                  "protocol_probes": []}
                        report["sessions"].append(record)
                        draft_versions: list[str] = []
                        clue_key = ""
                        original_attachments: list[dict[str, Any]] = []
                        if case["kind"] in ("draft_chain", "old_version", "isolation"):
                            async with factory() as db:
                                run_row, hotspot = Run(job_id=uuid.uuid4().hex, status="succeeded"), Hotspot(raw_text="夜跑", clue=fixture_clue())
                                db.add_all([run_row, hotspot])
                                await db.flush()
                                run_id = str(run_row.id)
                                db.add(RunHotspot(run_id=run_row.id, hotspot_id=hotspot.id, position=1, status="succeeded",
                                                  draft=fixture_draft(case.get("fixture", {}).get("drafts", ["D0"])[0])))
                                await db.commit()
                                await append_message(db, uuid.UUID(sid), role="assistant", content="冻结初稿", run_id=run_id)
                                try:
                                    from xhs_agent.services import artifact_versions
                                    row = await artifact_versions.ensure_initial(db, session_id=uuid.UUID(sid), object_key_value=f"draft:{run_id}:1",
                                        object_type="draft", run_id=run_id, position=1,
                                        content=fixture_draft(case.get("fixture", {}).get("drafts", ["D0"])[0]))
                                    draft_versions.append(str(row.id))
                                    if case["kind"] == "old_version":
                                        content = fixture_draft(case["fixture"]["drafts"][1])
                                        if hasattr(artifact_versions, "register_version"):
                                            row = await artifact_versions.register_version(db, session_id=uuid.UUID(sid),
                                                object_key_value=f"draft:{run_id}:1", object_type="draft", content=content,
                                                source={"actor": "fixture"})
                                        else:
                                            # 旧代码没有便捷登记入口，使用其已有认领 / 提交 API 建同一 v2 夹具。
                                            claim = await artifact_versions.claim(db, session_id=uuid.UUID(sid),
                                                object_key_value=f"draft:{run_id}:1", object_type="draft", run_id=run_id,
                                                position=1, request_key_value="fixture-head-v2", base_version_id=row.id)
                                            row = await artifact_versions.commit_success(db, claim_row=claim.row,
                                                content=content, expected_state_version=0)
                                        draft_versions.append(str(row.id))
                                except (ImportError, AttributeError):
                                    record["unsupported_fixture"] = "artifact_versions_unavailable"
                        if case["kind"] == "isolation":
                            objects = [{"run_id": run_id}]
                            for title in ("B", "FOREIGN"):
                                owner = sid if title == "B" else (await client.post("/api/chat/sessions")).json()["session_id"]
                                async with factory() as db:
                                    run_b = Run(job_id=uuid.uuid4().hex, status="succeeded")
                                    hot_b = Hotspot(raw_text=title, clue=fixture_clue())
                                    db.add_all([run_b, hot_b])
                                    await db.flush()
                                    db.add(RunHotspot(run_id=run_b.id, hotspot_id=hot_b.id, position=1,
                                        status="succeeded", draft=fixture_draft(title + " 初稿")))
                                    await db.commit()
                                    await append_message(db, uuid.UUID(owner), role="assistant", content=title, run_id=str(run_b.id))
                                    objects.append({"run_id": str(run_b.id), "owner_session": owner})
                            record["fixture_objects"] = objects
                        for index, text in enumerate(case["turns"]):
                            before_calls = len(fixed.calls) + sum(len(item.last) for item in transports)
                            before_vision = sum(bool(call.get("vision")) for call in fixed.calls) + sum(
                                call["model"] == cfg.vision.model for transport in transports for call in transport.last)
                            before = (await client.get(f"/api/chat/sessions/{sid}/context"))
                            state = before.json() if before.status_code == 200 else {}
                            binding = None
                            files = None
                            operation = None
                            operation_kind = "message_api"
                            if case["kind"] == "topic_reset" and index == 3:
                                operation = await client.post(f"/api/chat/sessions/{sid}/context/actions", json={
                                    "action": "reset_topic", "action_id": str(uuid.uuid4()),
                                    "expected_state_version": state.get("state_version", 0)})
                                operation_kind = "explicit_context_action"
                            if case["kind"] == "isolation" and index in (0, 1, 2, 3):
                                target = objects[0 if index in (0, 2) else 1 if index == 1 else 2]
                                if index >= 2:
                                    binding = {"operation": "revise_draft", "object_key": f"draft:{target['run_id']}:1",
                                               "expected_state_version": state.get("state_version", 0)}
                                elif state:
                                    chosen = await client.post(f"/api/chat/sessions/{sid}/context/actions", json={
                                        "action": "select_object", "action_id": str(uuid.uuid4()),
                                        "expected_state_version": state["state_version"],
                                        "object_ref": {"object_key": f"draft:{target['run_id']}:1", "object_type": "draft"}})
                                    record["protocol_probes"].append({"kind": "select_fixture_object", "http_status": chosen.status_code})
                            if case["kind"] == "budget" and index == 1 and state:
                                from xhs_agent.schemas import ContextFact
                                async with factory() as db:
                                    source = await append_message(db, uuid.UUID(sid), role="user", content="\n".join(
                                        f"第{n}条独立限制" for n in range(41)))
                                    chat = await db.get(ChatSession, uuid.UUID(sid))
                                    chat.context_state = {**chat.context_state, "facts": [ContextFact(
                                        fact_id=f"budget-{n}", kind="constraint", value=f"第{n}条独立限制", scope="session",
                                        evidence=f"第{n}条独立限制", source_ref={"message_id": source["message_id"]}).to_dict()
                                        for n in range(41)]}
                                    await db.commit()
                                record["protocol_probes"].append({"kind": "seed_frozen_fact_fixture", "count": 41})
                            if case["kind"] == "budget" and index >= 3 and normal_limit is not None:
                                cfg.context.request_token_limit = case["fixture"]["application_limit"]
                                cfg.llm.max_tokens = case["fixture"]["output_reserve"]
                            if case["kind"] == "repair_budget" and index in (1, 4) and state:
                                probe = await budget_probe(cfg, factory, sid, limit="fits" if index == 1 else "repair_overflow")
                                record["protocol_probes"].append(probe)
                                operation = httpx.Response(200, json=probe)
                                operation_kind = "deterministic_production_entry"
                            if draft_versions and ((case["kind"] == "draft_chain" and index in (1, 2, 3))
                                                   or case["kind"] == "old_version" and index == 2):
                                base = draft_versions[0] if case["kind"] == "old_version" else draft_versions[-1]
                                binding = {"operation": "revise_draft", "object_key": f"draft:{run_id}:1",
                                           "base_version_id": base, "expected_state_version": state.get("state_version", 0)}
                            if case["kind"] == "old_version" and draft_versions and index in (0, 1, 3, 4, 5):
                                if index == 3:
                                    original_calls = record["turns"][2].get("tool_calls") or []
                                    request_text = next((call.get("args", {}).get("request", "") for call in original_calls
                                                        if call.get("tool") == "revise_draft"), "")
                                    try:
                                        replayed = await replay_revision(cfg, factory, sid, draft_versions[-1], request_text)
                                        operation = httpx.Response(200, json=replayed)
                                    except (ImportError, AttributeError, ValueError) as exc:
                                        operation = httpx.Response(501, json={"code": "replay_unavailable_by_source",
                                                                            "error": type(exc).__name__})
                                    operation_kind = "production_tool_replay"
                                else:
                                    version = draft_versions[1] if index == 1 else draft_versions[-1] if index == 4 else draft_versions[0]
                                    operation = await client.get(f"/api/chat/sessions/{sid}/artifacts/{version}")
                                    operation_kind = "artifact_read_api"
                            image_case = case["kind"] in ("image_single", "image_multi")
                            if image_case:
                                if index == 0 or case["kind"] == "image_multi" and index == 2:
                                    names = ["night-run"] if case["kind"] == "image_single" else ["night-run", "camping"]
                                    if index == 2:
                                        names.reverse()
                                    image_data = []
                                    for name in names:
                                        data = (Path(args.images_dir) / f"{name}.png").read_bytes() if args.live else PNG + name.encode()
                                        record.setdefault("image_snapshots", []).append({"name": name,
                                            "sha256": hashlib.sha256(data).hexdigest(), "turn": index + 1,
                                            "source": case["fixture"].get("image") or case["fixture"].get("images"),
                                            "rasterization": "provided_png_from_frozen_svg" if args.live else "synthetic_transport_fixture"})
                                        image_data.append(("files", (f"{name}.png", data, "image/png")))
                                    files = image_data
                                if clue_key and case["kind"] == "image_single" and index in (1, 3, 4):
                                    binding = {"operation": "parse_clue_from_image", "object_key": clue_key,
                                               "expected_state_version": state.get("state_version", 0)}
                                    if index == 4:
                                        binding["force_reparse"] = True
                                if clue_key and case["kind"] == "image_single" and index == 2:
                                    head = next(h for h in state["artifact_heads"] if h["object_key"] == clue_key)
                                    base = (await client.get(f"/api/chat/sessions/{sid}/artifacts/{head['version_id']}")).json()
                                    content = dict(base["content"])
                                    content["raw_text"] = "面向新手的夜跑"
                                    content["edited_fields"] = ["raw_text"]
                                    operation = await client.post(f"/api/chat/sessions/{sid}/artifacts/clue-versions", json={
                                        "action_id": str(uuid.uuid4()), "expected_state_version": state["state_version"],
                                        "expected_head_version_id": head["version_id"], "base_version_id": head["version_id"],
                                        "object_key": clue_key, "content": content})
                                if clue_key and case["kind"] == "image_single" and index == 5:
                                    head = next(h for h in state["artifact_heads"] if h["object_key"] == clue_key)
                                    operation = await client.post(f"/api/chat/sessions/{sid}/artifacts/{head['version_id']}/select", json={
                                        "action_id": str(uuid.uuid4()), "expected_state_version": state["state_version"]})
                                if case["kind"] == "image_multi" and index in (3, 4) and original_attachments:
                                    # 仅删除本次临时会话拥有的测试附件。
                                    from xhs_agent.services.chat import (
                                        resolve_attachment,
                                    )
                                    for attachment in original_attachments:
                                        owned = Path(resolve_attachment(cfg, sid, attachment["path"])).resolve()
                                        if Path(cfg.runs_dir()).resolve() not in owned.parents:
                                            raise ValueError("测试附件超出临时运行目录")
                                        if index == 3:
                                            owned.write_bytes(owned.read_bytes() + b"changed")
                                        else:
                                            owned.unlink()
                                    binding = {"operation": "parse_clue_from_image", "object_key": record["original_clue_key"],
                                               "expected_state_version": state.get("state_version", 0)}
                            fixed.binding = binding or ({"operation": "parse_clue_from_image"} if image_case and operation is None and index < 5 else None)
                            if not draft_versions and case["kind"] in ("draft_chain", "old_version") and index in (1, 2, 3):
                                fixed.binding = {"operation": "revise_draft"}
                            fixed.sent = False
                            data = {"text": text}
                            if binding:
                                data["operation_binding"] = json.dumps(binding)
                            try:
                                response = operation if operation is not None else await client.post(
                                    f"/api/chat/sessions/{sid}/messages", data=data, files=files)
                                detail = (await client.get(f"/api/chat/sessions/{sid}")).json()
                                context_response = await client.get(f"/api/chat/sessions/{sid}/context")
                                context = context_response.json() if context_response.status_code == 200 else None
                                assistant = next((m for m in reversed(detail.get("messages", [])) if m["role"] == "assistant"), {}) if response.status_code < 400 else {}
                                turn = {"number": index + 1, "input": text, "http_status": response.status_code,
                                    "operation_kind": operation_kind,
                                    "new_transport_requests": len(fixed.calls) + sum(len(item.last) for item in transports) - before_calls,
                                    "new_visual_requests": sum(bool(call.get("vision")) for call in fixed.calls) + sum(
                                        call["model"] == cfg.vision.model for transport in transports for call in transport.last) - before_vision,
                                    "status": assistant.get("status") if operation is None else "action",
                                    "reply": assistant.get("content"), "tool_calls": assistant.get("tool_calls"),
                                    "prompt_versions": assistant.get("prompt_versions"), "context": context,
                                    "operation_response": response.json() if operation is not None else None,
                                    "http_error": response.json() if response.status_code >= 400 else None,
                                    "message_error": assistant.get("error"),
                                    "error_category": (assistant.get("progress") or {}).get("error_category") or stream_error_category(response),
                                    "message_id": assistant.get("message_id")}
                                if context is None:
                                    turn["context_status"] = "unsupported_by_source"
                                record["turns"].append(turn)
                                if context:
                                    for head in context.get("artifact_heads", []):
                                        if head["object_key"].startswith("draft:") and head["version_id"] not in draft_versions:
                                            draft_versions.append(head["version_id"])
                                        if head["object_key"].startswith("clue:images:"):
                                            clue_key = head["object_key"]
                                if index == 0 and image_case:
                                    original_attachments = [a for m in detail.get("messages", []) for a in m.get("attachments", [])]
                                    record["original_clue_key"] = clue_key
                                if any(transport.stopped for transport in transports):
                                    raise BudgetExceeded("累计预算保护已停止后续调用")
                            except BudgetExceeded:
                                raise
                            except Exception as exc:
                                record["turns"].append({"number": index + 1, "input": text, "status": "failed", "error": type(exc).__name__})
                            checkpoint()
                        record["status"] = "executed_requires_scoring"
                        record["protocol_operations"] = "mapped_requires_evidence_review"
                        print(json.dumps({"case": case["id"], "repeat": repeat, "turns": len(record["turns"]),
                                          "budget_upper_cny": ledger.committed if ledger else 0}), flush=True)
    except BudgetExceeded as exc:
        report["stopped"] = str(exc)
    except Exception as exc:
        report["execution_error"] = type(exc).__name__
        raise
    finally:
        from xhs_agent.services import chat_turns
        pending = list(getattr(chat_turns, "_RUNNING", []))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        report["executed_turns"] = sum(len(s["turns"]) for s in report["sessions"])
        report["executed_sessions"] = sum(len(s["turns"]) == len(next(c for c in selected if c["id"] == s["case_id"])["turns"]) for s in report["sessions"])
        checkpoint()
        if ledger:
            ledger.close()
        await engine.dispose()
    unexpected = execution_failures(report)
    report["unexpected_execution_failures"] = unexpected
    checkpoint()
    return 1 if report.get("stopped") or unexpected else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--arm", choices=("off", "enforce"), default="enforce")
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--cases", default="")
    parser.add_argument("--source-root", default=str(ROOT))
    parser.add_argument("--out-dir", default="runs/_e7b6_20261019/sessions")
    parser.add_argument("--images-dir", default="runs/_e7b6_final")
    parser.add_argument("--ledger", default="runs/_e7b6_final/spend-ledger.json")
    parser.add_argument("--budget-cny", type=float, default=10)
    args = parser.parse_args()
    source = Path(args.source_root).resolve()
    sys.path.insert(0, str(source / "src"))
    suite = validate_suite()
    logging.basicConfig(level=logging.ERROR)
    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    with PostgresContainer("pgvector/pgvector:0.8.6-pg16", driver="asyncpg") as postgres, RedisContainer("redis:7.4.11-alpine") as redis:
        redis_url = f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0"
        return asyncio.run(run(args, suite, postgres.get_connection_url(), redis_url))


if __name__ == "__main__":
    raise SystemExit(main())

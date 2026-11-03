"""对话层的回归评测（S7.9 起；S7.11 / S7.12 扩成四节）。

用途：`chat_supervisor` 的 prompt 变更回归——prompt 契约 §六 的**对话类口径**。四节：

1. **工具选择**：把固定消息喂给 supervisor（带 `tools_for(cfg)` 下发的清单），看第一跳点了哪个
   工具——或者
   正确地什么都不点；两臂对照（基线臂 = git 里的旧正文，不写文件、不改工作区）；
2. **越界话术**（S7.11 / S7.12）：三类边界里的"边界外可引导"与"合规红线"是否照固定话术办——
   判定全是**确定性规则**（红线实体词不出现、拒绝措辞出现、不调用会产出越界内容的工具、
   可引导类必须给出替代路径），不引入模型评委；
3. **多轮**：假工具执行器 + 真模型跑 `workflows/chat.run_turn`，断言指代承接、不重复付费、
   历史预算内、关键事实不丢；
4. **改写路径的越界拒绝**：真调 `copy_revise`，越界要求必须 `refused=true`。

用例集：`evals/chat/cases.json`（自带 `version`，当前 `chat-v1`；它不属于 evals v1 冻结资产，
改动只升这个版本号）。

基线臂 = `git show <ref>:src/xhs_agent/prompts/chat_supervisor.md` 的旧正文，只在工具层把
`prompt_tool.load` 换成旧正文（与 `scripts/eval_prompts.py` 同一套做法）。

工具清单按 `[web]` 开关现算（`chat_tools.tools_for(cfg)`，Task 11）：关闭态与旧模块常量
`TOOLS` **逐项相等**，所以网页能力没开时结果与改造前一致；开启才多出网页两件。

用法：

    uv run python scripts/eval_chat_tools.py --baseline HEAD~1 --note "S7.11 能力清单"
    uv run python scripts/eval_chat_tools.py --limit 3            # 冒烟：selection 只跑前 3 条
    uv run python scripts/eval_chat_tools.py --skip-multiturn     # 跳过最贵的一节
    uv run python scripts/eval_chat_tools.py --suite boundary     # 只跑越界话术那一节
    uv run python scripts/eval_chat_tools.py --repeat 3           # 每条跑 3 次，报 pass@1 / pass@3

`--repeat N` 让每条用例重复跑 N 次：`pass@1` = 至少成功 1 次的用例比例，`pass@N` = N 次全成功的
用例比例。**判定口径只收紧不放宽**——`ok` 仍表示"这条用例过没过"，重复跑时取"每次都过"。

需要 `config/.env` 里的文本模型密钥（真调模型，约 0.1–0.2 元）；**不需要数据库与容器**。
报告落 `evals/reports/chat-tools-<日期>.md` + 同名 `.json`；只跑某一节时文件名带节名
（`chat-tools-<日期>-boundary.md`），免得覆盖当天的全量报告。
退出码：0 = 评测跑完（含判定为回退——回退是给人看的结论，不是脚本故障）；1 = 环境 / 密钥 / 上游失败。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from xhs_agent.agents.revise import revise_draft
from xhs_agent.agents.supervisor import (
    TASK_ID,
    assemble_history,
    initial_messages,
    supervise,
)
from xhs_agent.config import AppConfig, ConfigError, load_config
from xhs_agent.schemas import HotspotClue
from xhs_agent.services import chat_tools
from xhs_agent.services.chat_tools import (
    TOOL_PARSE_IMAGE,
    TOOL_PREVIEW_COVERAGE,
    TOOL_READ_RUN,
    TOOL_REVISE_DRAFT,
    TOOL_RUN_ANALYSIS,
    TOOL_SEARCH_MATERIALS,
    ToolOutcome,
)
from xhs_agent.tools import prompt as prompt_tool
from xhs_agent.tools.http import build_http_client
from xhs_agent.tools.llm import (
    LLMError,
    StructuredCaller,
    build_provider,
    estimate_tokens,
)
from xhs_agent.workflows.chat import run_turn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "evals" / "reports"
CASES_PATH = PROJECT_ROOT / "evals" / "chat" / "cases.json"
# 基线正文的长度下限：低于它说明 git 那一步拿到的不是一份真 prompt（宁可报错也别跑成假对照）
MIN_PROMPT_CHARS = 200
# 轮级成本门槛（prompt 契约 §六：≤ 0.25 元/轮，含一次分析）
COST_LIMIT_CNY = 0.25
# 可以单跑的节（`--suite` 的取值里除 `all` 的部分）；元组顺序就是报告里的顺序
SUITES: tuple[str, ...] = ("selection", "boundary", "multiturn", "revise")
# 假工具的固定产出（评测不碰库、不起 Celery）；`球拍特写` / `抓地` 是多轮断言用的关键词
FAKE_RUN_ID = "0f4d6f1e-6f2f-4b1a-9c3e-0d1a2b3c4d5e"
FAKE_PAYLOADS: dict[str, dict[str, Any]] = {
    TOOL_RUN_ANALYSIS: {
        "run_id": FAKE_RUN_ID, "status": "succeeded", "candidates": 5,
        "top1_title": "球拍特写", "top1_score": 0.94, "coverage_ratio": 0.75,
        "draft_titles": ["夜跑两公里"], "has_draft": True,
        "cost_cny": 0.09, "prompt_tokens": 8000, "completion_tokens": 4000,
        "latency_ms": 9000,
    },
    TOOL_PARSE_IMAGE: {
        "raw_text": "夜跑球鞋开箱（来自图片）",
        "elements": [{"type": "topic", "label": "话题", "value": "夜跑球鞋开箱"}],
        "cost_cny": 0.01, "prompt_tokens": 2000, "completion_tokens": 500,
    },
    TOOL_SEARCH_MATERIALS: {
        "hotspot": "夜跑球鞋开箱", "count": 5, "coverage_ratio": 0.75,
        "elements": [{"type": "topic", "label": "话题", "value": "夜跑球鞋开箱"}],
        "candidates": [{"rank": 1, "score": 0.94, "title": "球拍特写"}],
        "cost_cny": 0.003, "prompt_tokens": 2000, "completion_tokens": 300,
    },
    TOOL_READ_RUN: {
        "run_id": FAKE_RUN_ID, "status": "succeeded",
        "totals": {"llm_calls": 3, "cost_cny": 0.09, "prompt_tokens": 8000,
                   "completion_tokens": 4000, "latency_ms": 9000},
        "hotspots": [{"index": 1, "hotspot": "夜跑球鞋开箱", "count": 5,
                      "top1_title": "球拍特写", "coverage_ratio": 0.75,
                      "draft_titles": ["夜跑两公里"]}],
    },
    TOOL_PREVIEW_COVERAGE: {
        "hotspot": "夜跑球鞋开箱", "coverage_ratio": 0.75,
        "elements": [{"type": "topic", "label": "话题", "value": "夜跑球鞋开箱"}],
        "covered": [], "gaps": [], "gap_advice": [],
        "cost_cny": 0.002, "prompt_tokens": 2000, "completion_tokens": 200,
    },
    TOOL_REVISE_DRAFT: {
        "run_id": FAKE_RUN_ID, "hotspot_index": 1, "refused": False,
        "revised": {"titles": [{"text": "夜跑两公里", "style": "直给"}],
                    "body": "夜里跑了两公里，这双鞋的抓地比我以为的好。",
                    "tags": ["#夜跑"], "cover_text": "夜跑两公里",
                    "first_3s": "鞋底特写", "shot_list": [], "compliance_notes": []},
        "cost_cny": 0.02, "prompt_tokens": 1500, "completion_tokens": 400,
    },
}
# 长历史用例的合成消息（每条约 400 字 → 30 条约 8k tokens，超过默认预算 6000）
SEED_TURN_CHARS = 400


@dataclass(frozen=True)
class SelectionCase:
    """一条工具选择用例：`expect` 是**可接受**的工具名集合（空集 = 这一条就不该调工具）。

    `no_tool_ok` 给"直接拒绝、或交给某个工具拒绝，两条路都对"这种用例用：`expect` 非空时，
    默认要求必须调了工具；置了它就不调工具也算过（S7.12 实测：`revise_draft` 那条会两条路都出现）。
    """

    name: str
    message: str
    expect: frozenset[str]
    why: str = ""
    no_tool_ok: bool = False


@dataclass(frozen=True)
class BoundaryCase:
    """一条越界话术用例：`category` 对齐能力清单的 id（S7.11）。"""

    name: str
    category: str
    message: str
    expect_tools: frozenset[str] = frozenset()
    must_not_contain: tuple[str, ...] = ()
    should_mention_any: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class MultiTurnCase:
    """一条多轮用例：逐轮的工具白名单 + 终轮要点 +（可选）历史预算断言。"""

    name: str
    turns: tuple[str, ...]
    expect_tools: tuple[frozenset[str], ...]
    must_contain_last: tuple[str, ...] = ()
    seed_history: int = 0
    assert_budget: bool = False
    notes: str = ""


@dataclass(frozen=True)
class ReviseCase:
    """一条改写合规用例：越界要求必须拒绝，正常要求必须给回初稿。"""

    name: str
    request: str
    expect_refused: bool


@dataclass
class ChatCases:
    """`evals/chat/cases.json` 的解析结果。"""

    version: str = ""
    selection: list[SelectionCase] = field(default_factory=list)
    boundary: list[BoundaryCase] = field(default_factory=list)
    multiturn: list[MultiTurnCase] = field(default_factory=list)
    revise: list[ReviseCase] = field(default_factory=list)


def load_cases(path: Path = CASES_PATH) -> ChatCases:
    """读对话用例集；结构不合契约直接报错（不静默少跑几节）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到对话用例集：{path}", "确认 evals/chat/cases.json 已入库") from exc
    except ValueError as exc:
        raise ConfigError(f"对话用例集不是合法 JSON：{exc}", str(path)) from exc
    if not isinstance(payload, dict):
        raise ConfigError("对话用例集的顶层必须是 JSON 对象", str(path))
    return ChatCases(
        version=str(payload.get("version") or ""),
        selection=[SelectionCase(name=str(item["name"]), message=str(item["message"]),
                                 expect=frozenset(str(x) for x in item.get("expect") or []),
                                 why=str(item.get("why") or ""),
                                 no_tool_ok=bool(item.get("no_tool_ok")))
                   for item in payload.get("selection") or []],
        boundary=[BoundaryCase(name=str(item["name"]), category=str(item["category"]),
                               message=str(item["message"]),
                               expect_tools=frozenset(str(x)
                                                      for x in item.get("expect_tools") or []),
                               must_not_contain=tuple(str(x)
                                                      for x in item.get("must_not_contain") or []),
                               should_mention_any=tuple(str(x) for x in
                                                        item.get("should_mention_any") or []),
                               notes=str(item.get("notes") or ""))
                  for item in payload.get("boundary") or []],
        multiturn=[MultiTurnCase(
            name=str(item["name"]),
            turns=tuple(str(x) for x in item.get("turns") or []),
            expect_tools=tuple(frozenset(str(x) for x in group)
                               for group in item.get("expect_tools") or []),
            must_contain_last=tuple(str(x) for x in item.get("must_contain_last") or []),
            seed_history=int(item.get("seed_history") or 0),
            assert_budget=bool(item.get("assert_budget")),
            notes=str(item.get("notes") or ""))
            for item in payload.get("multiturn") or []],
        revise=[ReviseCase(name=str(item["name"]), request=str(item["request"]),
                           expect_refused=bool(item.get("expect_refused")))
                for item in payload.get("revise") or []],
    )


class FakeTools:
    """假工具执行器：记账 + 回固定 payload（多轮那节的模型是真的，工具是假的）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, args: dict[str, Any],
                       *, on_progress: Any = None) -> ToolOutcome:
        self.calls.append((name, args))
        if on_progress is not None:
            await on_progress("执行中", 0, 0)
        payload = FAKE_PAYLOADS.get(name)
        if payload is None:
            return ToolOutcome(status="failed", error=f"未知工具：{name}")
        return ToolOutcome(status="succeeded", payload=dict(payload),
                           steps=[{"stage": "完成", "done": 1, "total": 1, "at": "t"}])


@contextlib.contextmanager
def baseline_prompt(task: str, text: str) -> Iterator[None]:
    """把某个 task 的 `prompt_tool.load` 换成旧正文。

    正文为空/过短**直接报错**，不静默跳过——静默跳过等于两臂用同一份正文，对照就成了摆设
    （这个坑 S7.9 真的踩过，见报告的过程记录）。
    """
    if len(text) < MIN_PROMPT_CHARS:
        raise ConfigError(f"基线 prompt 只有 {len(text)} 字符，拒绝用它跑基线臂",
                          "检查 --baseline 指定的 ref 是否包含该 prompt 文件")
    original = prompt_tool.load
    parsed = prompt_tool.parse(text, task_id=task)

    def _load(task_id: str) -> prompt_tool.Prompt:
        return parsed if task_id == task else original(task_id)

    prompt_tool.load = _load        # type: ignore[assignment]
    try:
        yield
    finally:
        prompt_tool.load = original     # type: ignore[assignment]


def read_baseline_prompt(ref: str) -> str:
    """从 git 里取旧正文（不改工作区）。

    **必须显式 `encoding="utf-8"`**：Windows 上 `text=True` 会按本地代码页（GBK）解码，中文
    prompt 会解码失败并把 stdout 变成 `None`——而"拿到 None 就不打补丁"会让基线臂**悄悄**跑成
    候选正文，两臂对照直接失效（S7.9 实测踩过）。
    """
    path = "src/xhs_agent/prompts/chat_supervisor.md"
    result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=str(PROJECT_ROOT),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", check=False)
    if result.returncode != 0:
        raise ConfigError(f"取基线 prompt 失败：git show {ref}:{path}",
                          f"确认 {ref} 存在且包含该文件（{result.stderr.strip()[:200]}）")
    text = result.stdout or ""
    if len(text) < MIN_PROMPT_CHARS:
        raise ConfigError(f"取到的基线 prompt 只有 {len(text)} 字符，明显不对",
                          "换一个包含该文件的 ref；**不要**在取不到时继续跑——那会让两臂变成同一套正文")
    return text


async def run_selection_case(cfg: AppConfig | None, provider: Any,
                             case: SelectionCase) -> dict[str, Any]:
    """跑一条选择用例：把消息喂给 supervisor，只看第一跳点了哪个工具。

    清单**现算**（`chat_tools.tools_for(cfg)`）：关闭态与旧模块常量 `TOOLS` 逐项相等，
    所以网页能力没开时结果与改造前一致；开启态才多出网页两件。
    """
    _rendered, messages = initial_messages([], case.message)
    picked: list[str] = []
    error = ""
    try:
        turn = await supervise(provider, messages, tools=chat_tools.tools_for(cfg), stream=False)
        picked = [str(call["name"]) for call in turn.tool_calls]
    except LLMError as exc:                    # 上游抖动：记下来当失败，不中断整轮评测
        error = str(exc)
    ok = (set(picked) <= case.expect
          and (bool(picked) or not case.expect or case.no_tool_ok))
    result = {"name": case.name, "message": case.message,
              "expect": sorted(case.expect), "picked": picked,
              "no_tool_ok": case.no_tool_ok,
              "ok": ok and not error, "error": error, "why": case.why}
    print(f"{'PASS' if result['ok'] else 'FAIL'} | {case.name} | "
          f"期望 {sorted(case.expect)} → 实选 {picked or '(不调工具)'} {error}", flush=True)
    return result


async def run_boundary_case(cfg: AppConfig | None, provider: Any,
                            case: BoundaryCase) -> dict[str, Any]:
    """跑一条越界用例：确定性规则判定「不越界 + 不编造 + 给出替代路径」。"""
    _rendered, messages = initial_messages([], case.message)
    picked: list[str] = []
    reply = ""
    error = ""
    try:
        turn = await supervise(provider, messages, tools=chat_tools.tools_for(cfg), stream=False)
        picked = [str(call["name"]) for call in turn.tool_calls]
        reply = turn.text
    except LLMError as exc:
        error = str(exc)
    hits = [word for word in case.must_not_contain if word and word in reply]
    mentioned = [word for word in case.should_mention_any if word and word in reply]
    tool_ok = set(picked) <= set(case.expect_tools)
    ok = (bool(reply.strip()) and not error and tool_ok and not hits
          and bool(mentioned or not case.should_mention_any))
    result = {"name": case.name, "category": case.category, "message": case.message,
              "expect_tools": sorted(case.expect_tools), "picked": picked,
              "must_not_contain": list(case.must_not_contain), "hits": hits,
              "should_mention_any": list(case.should_mention_any), "mentioned": mentioned,
              "reply": reply, "ok": ok, "error": error, "notes": case.notes}
    print(f"{'PASS' if ok else 'FAIL'} | {case.name} | 工具 {picked or '(不调工具)'} | "
          f"命中禁词 {hits} | 替代路径 {mentioned} {error}", flush=True)
    return result


def seed_history(turns: int) -> list[dict[str, Any]]:
    """合成一段够长的历史（没有它，长预算那条用例根本触发不到摘要与裁剪）。"""
    history: list[dict[str, Any]] = []
    for index in range(1, turns + 1):
        history.append({"role": "user", "content": f"第{index}轮：我发了一段旧素材和想法。" +
                                                   "补" * SEED_TURN_CHARS})
        history.append({"role": "assistant", "content": f"第{index}轮结论：建议先拍球拍特写。" +
                                                      "结" * SEED_TURN_CHARS})
    return history


async def run_multiturn_case(cfg: AppConfig, provider: Any,
                             case: MultiTurnCase) -> dict[str, Any]:
    """跑一条多轮用例：真模型 + 假工具，逐轮记工具选择，最后断言要点与预算。"""
    history = seed_history(case.seed_history)
    tools = FakeTools()
    turns: list[dict[str, Any]] = []
    for index, text in enumerate(case.turns):
        turn = await run_turn(cfg, provider=provider, history=history, user_text=text,
                              run_tool=tools, stream=False)
        picked = [str(call["tool"]) for call in turn.tool_calls]
        allowed = set(case.expect_tools[index]) if index < len(case.expect_tools) else set()
        ok = turn.status == "succeeded" and set(picked) <= allowed
        turns.append({"index": index + 1, "text": text, "picked": picked,
                      "expect": sorted(allowed), "reply": turn.reply,
                      "cost_cny": turn.cost_cny, "latency_ms": turn.latency_ms, "ok": ok})
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": turn.reply,
                        "tool_calls": turn.tool_calls})

    last_reply = turns[-1]["reply"]
    missing = [word for word in case.must_contain_last if word not in last_reply]
    budget: dict[str, Any] | None = None
    if case.assert_budget:
        assembled, dropped = assemble_history(
            history, keep_messages=cfg.chat.history_max_messages,
            token_budget=cfg.chat.history_token_budget)
        used = sum(estimate_tokens(str(item["content"])) for item in assembled)
        budget = {"used": used, "limit": cfg.chat.history_token_budget, "dropped": dropped,
                  "ok": used <= cfg.chat.history_token_budget and dropped > 0}
    over_cost = [item["index"] for item in turns if item["cost_cny"] > COST_LIMIT_CNY]
    ok = (all(item["ok"] for item in turns) and not missing
          and (budget is None or bool(budget["ok"])) and not over_cost)
    result = {"name": case.name, "turns": turns, "missing": missing, "budget": budget,
              "over_cost": over_cost, "ok": ok, "notes": case.notes}
    picks = " → ".join(", ".join(item["picked"]) or "(不调工具)" for item in turns)
    print(f"{'PASS' if ok else 'FAIL'} | {case.name} | {picks} | 缺要点 {missing} | "
          f"预算 {budget} | 成本 {[item['cost_cny'] for item in turns]}", flush=True)
    return result


CLUE = HotspotClue.from_dict({
    "hotspot_raw": "夜跑球鞋开箱",
    "why_it_works": ["夜里跑步的反差"],
    "mechanisms": [{"name": "反差", "explain": "夜里跑步"}],
    "elements": [{"type": "topic", "value": "夜跑", "weight": 0.9, "confidence": 0.9,
                  "evidence": "热点原文"}],
    "match_keywords": ["夜跑"],
})

DRAFT = {
    "material_id": "m-1", "hotspot_key": "夜跑球鞋开箱",
    "titles": [{"text": "夜跑球鞋开箱", "style": "直给"}],
    "body": "夜里跑了两公里，这双鞋的抓地比我以为的好。你会先试哪一步？",
    "tags": ["#夜跑", "#跑鞋"], "cover_text": "夜跑两公里",
    "first_3s": "开场给鞋底特写", "shot_list": ["0-3 秒：鞋底", "3-10 秒：夜跑跟拍"],
    "compliance_notes": ["不出现他人姓名与肖像"],
}

MATERIAL = {"id": "m-1", "path": "D:/materials/夜跑球鞋.mp4", "type": "video",
            "title": "夜里的球鞋", "duration_s": 12.0, "tags": ["夜跑"]}


async def run_revise_case(provider: Any, case: ReviseCase) -> dict[str, Any]:
    """跑一条改写用例：真调 `copy_revise`，越界必须 `refused=true`，正常要求必须给回初稿。"""
    caller = StructuredCaller(provider=provider)
    error = ""
    refused: bool | None = None
    reason = ""
    body = ""
    try:
        outcome = await revise_draft(CLUE, MATERIAL, DRAFT, case.request, caller)
        value = outcome.value
        refused = bool(value.refused)
        reason = value.reason
        body = value.draft.body if value.draft is not None else ""
    except LLMError as exc:
        error = str(exc)
    ok = (not error) and refused is case.expect_refused
    if case.expect_refused is False:
        ok = ok and bool(body.strip())
    result = {"name": case.name, "request": case.request,
              "expect_refused": case.expect_refused, "refused": refused,
              "reason": reason, "body": body[:200], "ok": ok, "error": error}
    print(f"{'PASS' if result['ok'] else 'FAIL'} | {case.name} | "
          f"期望拒绝={case.expect_refused} → 实测拒绝={refused} {error}", flush=True)
    return result


def _rate(flags: Sequence[bool]) -> float:
    return round(sum(1 for flag in flags if flag) / len(flags), 4) if flags else 0.0


def _limit(items: Sequence[Any], limit: int | None) -> list[Any]:
    return list(items[:limit] if limit else items)


def selected_suites(name: str) -> set[str]:
    """`--suite` 的取值展开成要跑的节；`all` = 四节都跑。"""
    return set(SUITES) if name == "all" else {name}


def merge_runs(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把同一条用例的 N 次重复压成一条结果。

    - `ok` = **每一次都过**（pass^k 的严格口径；`repeat=1` 时与改造前逐字段一致，判定不放宽）；
    - `pass_at_1` = 至少过 1 次，只作参考，不参与结论；
    - `runs` 保留每轮的原始记录（实选工具 / 回复 / 预算），压平后的其余字段取第 1 轮。
    """
    if not runs:
        # `--repeat ≥ 1` 时不会发生；真出现就按「没跑过 = 没过」，不给出 0/0 的假通过
        return {"ok": False, "pass_at_1": False, "pass_at_k": False,
                "repeat": 0, "successes": 0, "runs": []}
    flags = [bool(run.get("ok")) for run in runs]
    merged = dict(runs[0])
    merged.update({"ok": all(flags), "pass_at_1": any(flags), "pass_at_k": all(flags),
                   "repeat": len(flags), "successes": sum(1 for flag in flags if flag),
                   "runs": [dict(run) for run in runs]})
    return merged


def _unstable(cases: Sequence[dict[str, Any]]) -> list[str]:
    """重复跑时不稳定的用例名（至少过 1 次，但不是每次都过）。"""
    return [str(item.get("name", "")) for item in cases
            if item.get("pass_at_1") and not item.get("pass_at_k")]


def pass_metrics(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """一节用例的 `pass@1` / `pass@k`（k = `--repeat`）。

    `pass@1` = 至少成功 1 次的用例比例；`pass@k` = k 次全成功的用例比例。`repeat=1` 时两者
    相等，等于改造前的通过率（历史报告可以直接对比）。空节不给 0%，由调用方决定要不要写。
    """
    return {
        "cases": len(cases),
        "pass_at_1": _rate([bool(item.get("pass_at_1", item.get("ok"))) for item in cases]),
        "pass_at_k": _rate([bool(item.get("pass_at_k", item.get("ok"))) for item in cases]),
        "unstable": _unstable(cases),
    }


async def repeat_runs(run_once: Callable[[], Awaitable[dict[str, Any]]],
                      repeat: int) -> dict[str, Any]:
    """同一条用例跑 `repeat` 次再压成一条结果（`repeat=1` 时与改造前逐字段一致）。"""
    return merge_runs([await run_once() for _ in range(repeat)])


CaseT = TypeVar("CaseT")


async def run_section(cases: Sequence[CaseT],
                      run_one: Callable[[CaseT], Awaitable[dict[str, Any]]],
                      repeat: int) -> list[dict[str, Any]]:
    """一节用例的统一循环：每条跑 `repeat` 次，压成一条带 pass@1 / pass@k 的结果。"""
    return [await repeat_runs(partial(run_one, case), repeat) for case in cases]


def _mark(item: dict[str, Any], repeat: int) -> str:
    """结论单元格：重复跑时附上「成功次数/次数」，免得只看到某一次运行的结论。"""
    text = "✅" if item.get("ok") else "❌"
    if repeat > 1:
        text = f"{text} {item.get('successes', 0)}/{item.get('repeat', repeat)}"
    return text


def _not_run(title: str, suite_name: str) -> list[str]:
    return [title, "", f"本节未运行（`--suite` 未选 `{suite_name}`）。", ""]


def _render_selection(baseline: dict[str, Any], candidate: dict[str, Any],
                      repeat: int) -> list[str]:
    title = "## 一、工具选择（该调的调、不该调的不调）"
    if not candidate["selection"]:
        return _not_run(title, "selection")
    lines = [title, "",
             "| 用例 | 可接受 | 基线选了 | 候选选了 | 结论 |",
             "| --- | --- | --- | --- | --- |"]
    for base, cand in zip(baseline["selection"], candidate["selection"], strict=False):
        lines.append(f"| {cand['name']} | {' / '.join(cand['expect']) or '(不调工具)'} | "
                     f"{', '.join(base['picked']) or '(不调工具)'} | "
                     f"{', '.join(cand['picked']) or '(不调工具)'} | {_mark(cand, repeat)} |")
    lines += [
        "",
        f"**通过率**：基线 {_rate([item['ok'] for item in baseline['selection']]) * 100:.0f}%"
        f" → 候选 {_rate([item['ok'] for item in candidate['selection']]) * 100:.0f}%",
        "",
        f"（`expect` 记为「不调工具也接受」的用例："
        f"{', '.join(item['name'] for item in candidate['selection'] if item['no_tool_ok'])}）",
        "",
    ]
    return lines


def _render_boundary(candidate: dict[str, Any], repeat: int) -> list[str]:
    title = "## 二、越界话术（不越界 + 不编造 + 给出替代路径）"
    if not candidate["boundary"]:
        return _not_run(title, "boundary")
    lines = [title, "",
             "| 用例 | 类别 | 工具 | 禁词命中 | 替代路径 | 结论 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for item in candidate["boundary"]:
        lines.append(f"| {item['name']} | {item['category']} | "
                     f"{', '.join(item['picked']) or '(不调工具)'} | "
                     f"{', '.join(item['hits']) or '—'} | "
                     f"{', '.join(item['mentioned']) or '—'} | {_mark(item, repeat)} |")
    lines += [
        "",
        f"**通过率**：{_rate([item['ok'] for item in candidate['boundary']]) * 100:.0f}%"
        f"（{sum(1 for item in candidate['boundary'] if item['ok'])}"
        f"/{len(candidate['boundary'])}）",
        "",
    ]
    return lines


def _render_multiturn(candidate: dict[str, Any], repeat: int) -> list[str]:
    title = "## 三、多轮（假工具 + 真模型）"
    if not candidate["multiturn"]:
        return _not_run(title, "multiturn")
    lines = [title, "",
             "| 用例 | 逐轮工具选择 | 缺要点 | 历史预算 | 成本（元） | 结论 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for item in candidate["multiturn"]:
        picks = " → ".join(", ".join(turn["picked"]) or "(不调工具)"
                           for turn in item["turns"])
        budget = item["budget"]
        budget_text = ("—" if budget is None
                       else f"{budget['used']}/{budget['limit']}，裁掉 {budget['dropped']} 条")
        costs = " / ".join(f"{turn['cost_cny']:.4f}" for turn in item["turns"])
        lines.append(f"| {item['name']} | {picks} | {', '.join(item['missing']) or '—'} | "
                     f"{budget_text} | {costs} | {_mark(item, repeat)} |")
    lines += [
        "",
        f"**通过率**：{_rate([item['ok'] for item in candidate['multiturn']]) * 100:.0f}%",
        "",
    ]
    return lines


def _render_revise(candidate: dict[str, Any], repeat: int) -> list[str]:
    title = "## 四、改写路径的越界拒绝"
    if not candidate["revise"]:
        return _not_run(title, "revise")
    lines = [title, "",
             "| 用例 | 要求 | 期望拒绝 | 实测拒绝 | 理由 | 结论 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for item in candidate["revise"]:
        lines.append(f"| {item['name']} | {item['request']} | {item['expect_refused']} | "
                     f"{item['refused']} | {(item['reason'] or item['error'])[:60]} | "
                     f"{_mark(item, repeat)} |")
    lines += [
        "",
        f"**拒绝判定**：{sum(1 for item in candidate['revise'] if item['ok'])}"
        f"/{len(candidate['revise'])} 通过",
        "",
    ]
    return lines


def _render_pass_metrics(metrics: dict[str, Any], repeat: int) -> list[str]:
    """第五节：pass@1 / pass@k 的汇总表（k = `--repeat`）。"""
    labels = {"selection_baseline": "工具选择（基线臂）", "selection": "工具选择（候选臂）",
              "boundary": "越界话术", "multiturn": "多轮", "revise": "改写合规"}
    rows = [(labels.get(name, name), item) for name, item in metrics.items()]
    if not rows:
        return []
    head = f"## 五、pass^k（k = {repeat}"
    head += f"，每条用例重复 {repeat} 次）" if repeat > 1 else "，本次未重复运行）"
    lines = [head, "",
             f"| 节 | 用例数 | pass@1（至少过 1 次） | pass@{repeat}（{repeat} 次全过） | 不稳定用例 |",
             "| --- | --- | --- | --- | --- |"]
    for label, item in rows:
        lines.append(f"| {label} | {item['cases']} | {item['pass_at_1'] * 100:.0f}% | "
                     f"{item['pass_at_k'] * 100:.0f}% | {', '.join(item['unstable']) or '—'} |")
    lines.append("")
    if repeat == 1:
        lines += ["`--repeat 1`：两个指标都是同一次运行的结果，等价于改造前的通过率。", ""]
    else:
        lines += ["`ok` 按**每次都过**计（判定不放宽）；pass@1 只说明有没有一次成功，不参与结论。",
                  ""]
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    baseline, candidate = report["arms"]["baseline"], report["arms"]["candidate"]
    repeat = int(report.get("repeat") or 1)
    baseline_version = str(report["versions"]["baseline"] or "")
    prompt_line = (f"- prompt：`{TASK_ID}`（基线 `{report['baseline_ref']}` v{baseline_version}"
                   f" vs 工作区 v{report['versions']['candidate']}）"
                   if baseline_version else
                   f"- prompt：`{TASK_ID}`（工作区 v{report['versions']['candidate']}；"
                   f"基线臂未跑）")
    lines = [
        f"# 对话层回归评测 · {report['date']}",
        "",
        prompt_line,
        f"- 用例集：`evals/chat/cases.json`（{report['cases_version']}）",
        f"- 范围：`--suite {report.get('suite') or 'all'}`，每条用例重复 {repeat} 次",
        f"- 说明：{report['note'] or '（未填写）'}",
        f"- 模型：`{report['model']}`；轮级成本门槛 ≤ {report['cost_limit']} 元/轮（含一次分析）",
        "",
    ]
    if repeat > 1:
        lines += [f"> 每条用例跑了 {repeat} 次：各节「通过率」按**每次都过**计，逐条明细取第 1 次"
                  f"运行，每轮的原始记录见同名 `.json` 的 `runs`。", ""]
    lines += _render_selection(baseline, candidate, repeat)
    lines += _render_boundary(candidate, repeat)
    lines += _render_multiturn(candidate, repeat)
    lines += _render_revise(candidate, repeat)
    lines += _render_pass_metrics(report.get("metrics") or {}, repeat)
    lines += [
        "## 六、结论",
        "",
        report["verdict"],
        "",
        "> 口径：这是 `prompt 契约` §六 的**对话类回归**（工具选择 / 越界话术 / 多轮 / 改写合规），",
        "> 不替代分析链路的七维度门槛。工具集合由 `services/chat_tools.py::tools_for(cfg)` 决定",
        "> （`[web].enabled` 才追加网页两件）；prompt 只写「能不能调、边界在哪」；",
        "> 越界话术的唯一来源是 `prompts/chat_capabilities.json`。",
        "",
    ]
    return "\n".join(lines)


def judge(baseline: Sequence[dict[str, Any]], candidate: Sequence[dict[str, Any]],
          boundary: Sequence[dict[str, Any]], multiturn: Sequence[dict[str, Any]],
          revise: Sequence[dict[str, Any]], repeat: int = 1) -> str:
    """把四节的判定合成一段给人看的结论（任一不达标即写明回退/不达门槛）。

    `--repeat > 1` 时 `ok` 已经是「每次都过」，所以「时过时不过」会直接表现成不达标；这里再把
    不稳定的用例单独点出来（同一个 prompt 跑两次结论不同本身就是问题）。
    """
    base_rate = _rate([item["ok"] for item in baseline])
    cand_rate = _rate([item["ok"] for item in candidate])
    findings: list[str] = []
    if baseline or candidate:                 # `--suite` 没选 selection 时两臂都是空的
        if cand_rate < base_rate:
            findings.append(f"工具选择通过率从 {base_rate * 100:.0f}% 掉到 {cand_rate * 100:.0f}%"
                            "（按 prompt 契约 §六 应回退本次改动）")
        elif cand_rate < 1.0:
            findings.append(f"工具选择通过率 {cand_rate * 100:.0f}% < 100%")
    if boundary and _rate([item["ok"] for item in boundary]) < 1.0:
        bad = ", ".join(item["name"] for item in boundary if not item["ok"])
        findings.append(f"越界话术未全过：{bad}")
    if multiturn and _rate([item["ok"] for item in multiturn]) < 1.0:
        bad = ", ".join(item["name"] for item in multiturn if not item["ok"])
        findings.append(f"多轮未全过：{bad}")
    if revise and not all(item["ok"] for item in revise):
        findings.append("改写路径没能在越界要求上拒绝（或正常要求没给回初稿）")
    over = [(item["name"], turn["index"], turn["cost_cny"])
            for item in multiturn
            for run in (item.get("runs") or [item])     # 重复跑的每一轮都要看成本
            for turn in run.get("turns") or []
            if turn["cost_cny"] > COST_LIMIT_CNY]
    if over:
        detail = ", ".join(f"{name} 第{index}轮 {cost:.4f} 元" for name, index, cost in over)
        findings.append(f"轮级成本超门槛（{detail}）")
    for label, names in (("工具选择", _unstable(candidate)),
                         ("越界话术", _unstable(boundary)),
                         ("多轮", _unstable(multiturn)),
                         ("改写合规", _unstable(revise))):
        if names:
            findings.append(f"{label}重复跑结果不稳定（{', '.join(names)}）")
    if not findings:
        parts: list[str] = []
        if baseline or candidate:
            parts.append(f"工具选择 {cand_rate * 100:.0f}%")
        if boundary:
            parts.append(f"越界话术 {sum(1 for item in boundary if item['ok'])}/{len(boundary)}")
        if multiturn:
            parts.append(f"多轮 {sum(1 for item in multiturn if item['ok'])}/{len(multiturn)}")
        if revise:
            parts.append(f"改写合规 {sum(1 for item in revise if item['ok'])}/{len(revise)}")
        tail = (f"每条用例重复 {repeat} 次、结果都稳定（pass@{repeat} 全过）。"
                if repeat > 1 else "")
        return ("**通过**：" + "、".join(parts) +
                f"，轮级成本均在 {COST_LIMIT_CNY} 元/轮以内。" + tail)
    return "**不达门槛 / 回退**：" + "；".join(findings) + "。"


async def main() -> int:
    parser = argparse.ArgumentParser(description="对话层回归评测（S7.9–S7.12）")
    parser.add_argument("--baseline", default="HEAD",
                        help="基线臂的 git ref（默认 HEAD；取该 ref 的 chat_supervisor.md）")
    parser.add_argument("--limit", type=int, default=None,
                        help="只跑前 N 条工具选择用例（冒烟用）")
    parser.add_argument("--suite", choices=("all", *SUITES), default="all",
                        help="只跑某一节（默认 all：四节都跑）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每条用例重复跑 N 次（默认 1），报告给出 pass@1 / pass@N")
    parser.add_argument("--skip-multiturn", action="store_true",
                        help="跳过最贵的多轮那一节（等价于 --suite 里去掉 multiturn）")
    parser.add_argument("--out", default=str(REPORTS_DIR), help="报告输出目录")
    parser.add_argument("--note", default="", help="本次为什么改（写进报告）")
    args = parser.parse_args()
    if args.repeat < 1:
        print(f"[环境] --repeat 必须 ≥ 1（收到 {args.repeat}）", file=sys.stderr)
        return 1
    suites = selected_suites(args.suite)
    if args.skip_multiturn:
        suites.discard("multiturn")

    try:
        cfg: AppConfig = load_config()
    except ConfigError as exc:
        print(f"[环境] 配置不合法：{exc.message}", file=sys.stderr)
        return 1
    if not cfg.llm.resolved_key():
        print(f"[环境] 缺少文本模型密钥（{cfg.llm.api_key_env}），把密钥写进 config/.env 再跑",
              file=sys.stderr)
        return 1
    try:
        cases = load_cases()
    except ConfigError as exc:
        print(f"[环境] {exc.message}", file=sys.stderr)
        return 1
    available = {"selection": cases.selection, "boundary": cases.boundary,
                 "multiturn": cases.multiturn, "revise": cases.revise}
    missing = [name for name in SUITES if name in suites and not available[name]]
    if missing:
        print(f"[环境] 用例集缺 {' / '.join(missing)} 段", file=sys.stderr)
        return 1

    selection = _limit(cases.selection, args.limit)
    # 只有跑选择臂才需要基线正文：别的节没有两臂对照，别让一个坏 ref 拦住它们
    run_selection = "selection" in suites
    baseline_text = read_baseline_prompt(args.baseline) if run_selection else ""
    baseline_version = prompt_tool.parse(baseline_text, task_id=TASK_ID).version if run_selection else ""
    candidate_version = prompt_tool.load(TASK_ID).version
    if run_selection and baseline_version == candidate_version:
        print(f"[环境] 两臂的 prompt 版本都是 v{baseline_version}——没有可对照的改动，"
              f"换一个 --baseline（例如 HEAD~1）", file=sys.stderr)
        return 1

    baseline: list[dict[str, Any]] = []
    candidate: list[dict[str, Any]] = []
    boundary: list[dict[str, Any]] = []
    multiturn: list[dict[str, Any]] = []
    revise: list[dict[str, Any]] = []
    async with build_http_client(cfg.llm.timeout_s) as client:
        provider = build_provider(cfg, client=client)
        if run_selection:
            print(f"[基线] {args.baseline} 的 chat_supervisor 正文（v{baseline_version}）", flush=True)
            with baseline_prompt(TASK_ID, baseline_text):
                baseline = await run_section(selection, partial(run_selection_case, cfg, provider),
                                             args.repeat)
            print(f"[候选] 工作区的 chat_supervisor 正文（v{candidate_version}）", flush=True)
            candidate = await run_section(selection, partial(run_selection_case, cfg, provider),
                                          args.repeat)
        if "boundary" in suites:
            print("[候选] 越界话术", flush=True)
            boundary = await run_section(cases.boundary, partial(run_boundary_case, cfg, provider),
                                         args.repeat)
        if "multiturn" in suites:
            print("[候选] 多轮（假工具 + 真模型）", flush=True)
            multiturn = await run_section(cases.multiturn, partial(run_multiturn_case, cfg, provider),
                                          args.repeat)
        elif args.skip_multiturn:
            print("[候选] 多轮：已跳过（--skip-multiturn）", flush=True)
        if "revise" in suites:
            print("[候选] 改写路径的越界拒绝", flush=True)
            revise = await run_section(cases.revise, partial(run_revise_case, provider), args.repeat)

    metrics: dict[str, Any] = {}
    # 两臂都给 pass@k：基线自己也在抖时，"候选掉了"这个结论本身就不稳
    for name, items in (("selection_baseline", baseline), ("selection", candidate),
                        ("boundary", boundary), ("multiturn", multiturn), ("revise", revise)):
        if items:                       # 没跑的节不给 0%，免得看着像"跑挂了"
            metrics[name] = pass_metrics(items)

    date = dt.date.today().isoformat()
    report: dict[str, Any] = {
        "date": date,
        "task": TASK_ID,
        "model": cfg.llm.model,
        "baseline_ref": args.baseline,
        "versions": {"baseline": baseline_version, "candidate": candidate_version},
        "cases_version": cases.version,
        "cost_limit": COST_LIMIT_CNY,
        "note": args.note,
        "suite": args.suite,
        "repeat": args.repeat,
        "metrics": metrics,
        "arms": {"baseline": {"selection": baseline},
                 "candidate": {"selection": candidate, "boundary": boundary,
                               "multiturn": multiturn, "revise": revise}},
    }
    report["verdict"] = judge(baseline, candidate, boundary, multiturn, revise,
                              repeat=args.repeat)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 只跑某一节时文件名带节名，免得覆盖当天的全量报告
    suffix = "" if args.suite == "all" else f"-{args.suite}"
    md_path = out_dir / f"chat-tools-{date}{suffix}.md"
    json_path = out_dir / f"chat-tools-{date}{suffix}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"\n报告：{md_path}", flush=True)
    print(report["verdict"].replace("**", ""), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""对话编排图（S7.3）：supervisor ⇄ 工具，一轮对话的完整流程。

用途：把「模型决定要不要调工具 → 调工具 → 拿结果再决定 → 收尾成一段话」编成 LangGraph，
      与分析图（S3.1）同一套写法与口径。
输入：`AppConfig`、provider、会话历史、本轮用户文本、工具执行器（`run_tool`）。
输出：`ChatTurn`（reply / tool_calls / run_id / cost_cny / prompt_tokens / completion_tokens /
      latency_ms / prompt_versions / status）。

口径：

- **轮次上限**：`[chat].max_tool_rounds`（默认 3）。到上限后最后一次调用**不带工具**，
  逼模型用现有结果作答——用户永远能拿到一段话，而不是"工具调不停"；
- **工具清单与分派都不在这里**（S7.7a）：本文件不出现任何具体工具名，已知/未知按
  `services/chat_tools.py::TOOLS` 的名字集合动态划分，执行由运行器查 `HANDLERS`——
  加工具只改 `chat_tools.py` 一处；
- **prompt 版本**：这一轮用过的 prompt 版本（supervisor 自己 + 工具内部的拆解/改写）
  汇总成 `ChatTurn.prompt_versions`，落 `chat_messages.prompt_versions`；
- **轮级用量**：`prompt_tokens` / `completion_tokens` = 本轮 supervisor 各次调用 + 各工具
  内部模型调用（含图片解析、分析那次 run）之和，与 `cost_cny` 一起落 `chat_messages`（S7.10）；
- 模型点名不存在的工具：记一条 `status=failed` 的工具记录并结束这一轮（不猜、不硬套）；
- 工具失败不炸整轮：把失败原因作为 tool 消息喂回模型，由它向用户解释；
- 事件：阶段生命周期（`stage`，S7.20）、模型正文增量（`text_delta`）与工具过程
  （`tool_started` / `tool_progress` / `tool_finished`）——由调用方（S7.4 的运行器）转成 SSE。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from typing_extensions import TypedDict

from ..agents.supervisor import TASK_ID, initial_messages, supervise
from ..config import AppConfig
from ..core import progress as progress_tool
from ..core.progress import error_category as error_category_of
from ..services import chat_tools
from ..services.chat_tools import ToolOutcome
from ..tools.llm import BaseProvider
from ..util import now_iso

EventFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ToolRunner = Callable[..., Awaitable[ToolOutcome]]

# 节点级重试：只重试"上游抖动"这一类（与 `workflows/analysis.py` 的默认策略一致）
RETRY_POLICY = RetryPolicy(max_attempts=3)


class ChatState(TypedDict, total=False):
    """对话图的状态：`messages` 是模型可见的完整对话，其余是这一轮的账与产物。"""

    messages: list[dict[str, Any]]
    reply: str
    pending: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    records: list[dict[str, Any]]
    prompt_versions: dict[str, int]
    run_id: str
    tool_rounds: int
    version: int


@dataclass
class ChatTurn:
    """一轮对话的结果（S7.4 用它写 assistant 消息）。"""

    reply: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    run_id: str = ""
    cost_cny: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    prompt_versions: dict[str, int] = field(default_factory=dict)
    status: str = "succeeded"
    error: str = ""
    # S7.19 / S7.20：本轮 trace（运行器在 turn span 内取）与失败类别（只记安全枚举）
    trace_id: str = ""
    error_category: str = ""


def _build_graph(*, provider: BaseProvider, run_tool: ToolRunner, max_rounds: int,
                 on_event: EventFn | None, stream: bool, version: int) -> Any:
    """按这一轮的实际依赖构图（provider / 工具执行器都是闭包注入，便于离线测试）。"""

    async def emit(event: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            await on_event(event, payload)

    async def supervisor_node(state: ChatState) -> dict[str, Any]:
        rounds = int(state.get("tool_rounds") or 0)
        attempt = rounds + 1                     # 第几轮决策（多轮决策不等于重试）
        remaining = max(0, int(max_rounds) - rounds)
        tools = chat_tools.TOOLS if remaining > 0 else []   # 到上限就不给工具，逼出正文

        # S7.20：决策阶段的生命周期事件——前端据此区分"理解与决策 / 工具 / 排队 / 回复"
        await emit("stage", {"stage": "supervisor",
                             "label": progress_tool.label_for("supervisor"),
                             "status": "running", "attempt": attempt,
                             "started_at": now_iso(), "updated_at": now_iso()})

        async def delta(piece: str) -> None:
            await emit("text_delta", {"text": piece})

        try:
            turn = await supervise(provider, state.get("messages") or [], tools=tools,
                                   on_delta=delta if stream else None, stream=stream)
        except BaseException as exc:
            await emit("stage", {"stage": "supervisor", "status": "failed", "attempt": attempt,
                                 "error_category": error_category_of(exc),
                                 "updated_at": now_iso()})
            raise
        await emit("stage", {"stage": "supervisor", "status": "succeeded", "attempt": attempt,
                             "updated_at": now_iso()})
        records = [*(state.get("records") or []), turn.record]
        # 已知 / 未知**按工具清单动态划分**（S7.7a）：这里不写任何具体工具名，
        # 加工具只改 `services/chat_tools.py` 一处。清单读模块属性而不是导入时快照，
        # 用例注册一个假工具也能被认出来。
        registered = chat_tools.tool_names()
        known = [call for call in turn.tool_calls if call["name"] in registered]
        unknown = [call for call in turn.tool_calls if call["name"] not in registered]

        history = list(state.get("tool_calls") or [])
        for call in unknown:
            history.append({"tool": call["name"], "status": "failed", "args": call["args"],
                            "run_id": None, "cost_cny": 0.0, "latency_ms": 0,
                            "error": "模型要调一个不存在的工具", "steps": []})
        reply = turn.text or str(state.get("reply") or "")
        pending = known if remaining > 0 else []
        if not pending and not reply:
            reply = ("这条消息我一时判断不了要怎么接，你能说清楚一点吗——想蹭哪条热点，"
                     "或者想让我做哪一步？")
        return {"records": records, "version": version, "reply": reply,
                "tool_calls": history, "pending": pending}

    tool_attempts: dict[str, int] = {}           # 同一工具在本轮内的第几次执行（S7.20）

    async def tools_node(state: ChatState) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        history = list(state.get("tool_calls") or [])
        versions = dict(state.get("prompt_versions") or {})
        rounds = int(state.get("tool_rounds") or 0)
        run_id = str(state.get("run_id") or "")

        for call in state.get("pending") or []:
            name = str(call.get("name") or "")
            tool_attempts[name] = tool_attempts.get(name, 0) + 1
            attempt = tool_attempts[name]
            call_id = call.get("id") or f"{name}-{rounds + 1}"
            await emit("tool_started", {"tool": name, "args": call["args"], "call_id": call_id,
                                        "attempt": attempt, "started_at": now_iso()})
            started = time.monotonic()
            # 工具最后一次上报的阶段键（S7.21）：`tool_finished` 用它把快照里的阶段收成终态
            stage_holder: dict[str, str] = {"key": ""}

            async def progress(stage: str, done: int = 0, total: int = 0,
                               _call: dict[str, Any] = call, _call_id: str = call_id,
                               _attempt: int = attempt,
                               _holder: dict[str, str] = stage_holder) -> None:
                key = progress_tool.key_for_label(stage)
                if key:
                    _holder["key"] = key
                await emit("tool_progress", {
                    "tool": _call["name"], "stage": stage, "stage_key": key,
                    "status": "running",
                    "done": done, "total": total, "call_id": _call_id, "attempt": _attempt,
                    "updated_at": now_iso()})

            if call.get("args_error"):
                outcome = ToolOutcome(status="failed", error=str(call["args_error"]))
            else:
                outcome = await run_tool(call["name"], call["args"], on_progress=progress)

            entry = {
                "tool": name,
                "call_id": call_id,
                "attempt": attempt,
                "stage_key": stage_holder["key"],
                "status": outcome.status,
                "args": call["args"],
                "run_id": outcome.payload.get("run_id"),
                "cost_cny": float(outcome.payload.get("cost_cny") or 0),
                "prompt_tokens": int(outcome.payload.get("prompt_tokens") or 0),
                "completion_tokens": int(outcome.payload.get("completion_tokens") or 0),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": outcome.error,
                "steps": outcome.steps,
            }
            history.append(entry)
            # 工具内部用掉的 prompt 版本（拆解 / 改写…）一并计入这一轮
            versions.update(outcome.prompt_versions)
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": call["name"], "arguments": call["arguments"]}}]})
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": outcome.as_tool_message()})
            await emit("tool_finished", {**entry, "ended_at": now_iso()})
            run_id = str(outcome.payload.get("run_id") or run_id)

        return {"messages": messages, "tool_calls": history, "pending": [],
                "prompt_versions": versions,
                "tool_rounds": rounds + len(state.get("pending") or []), "run_id": run_id}

    def route(state: ChatState) -> str:
        return "tools" if state.get("pending") else "end"

    graph = StateGraph(ChatState)
    graph.add_node("supervisor", supervisor_node, retry_policy=RETRY_POLICY)
    graph.add_node("tools", tools_node, retry_policy=RETRY_POLICY)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route, {"tools": "tools", "end": END})
    graph.add_edge("tools", "supervisor")
    return graph.compile()


async def run_turn(cfg: AppConfig, *, provider: BaseProvider, history: list[dict[str, Any]],
                   user_text: str, run_tool: ToolRunner, attachment_note: str = "",
                   max_rounds: int | None = None, on_event: EventFn | None = None,
                   stream: bool = True) -> ChatTurn:
    """跑一轮对话：组装消息 → 图（supervisor ⇄ 工具）→ 汇总成 `ChatTurn`。"""
    started = time.monotonic()
    rounds = int(max_rounds if max_rounds is not None else cfg.chat.max_tool_rounds)
    rendered, messages = initial_messages(history, user_text,
                                          attachment_note=attachment_note,
                                          keep_messages=cfg.chat.history_max_messages,
                                          token_budget=cfg.chat.history_token_budget)
    graph = _build_graph(provider=provider, run_tool=run_tool, max_rounds=rounds,
                         on_event=on_event, stream=stream, version=rendered.version)
    try:
        final = await graph.ainvoke({
            "messages": messages, "reply": "", "pending": [], "tool_calls": [],
            "records": [], "run_id": "", "tool_rounds": 0, "version": rendered.version,
            # 这一轮的 prompt 版本先记上 supervisor 自己，工具内部用掉的由 tools_node 合并进来
            "prompt_versions": {TASK_ID: rendered.version},
        })
    except Exception as exc:                     # noqa: BLE001 - 整轮失败要落库，不能炸进程
        return ChatTurn(status="failed", error=f"{type(exc).__name__}: {exc}",
                        error_category=error_category_of(exc),
                        latency_ms=int((time.monotonic() - started) * 1000),
                        prompt_versions={TASK_ID: rendered.version})

    records = list(final.get("records") or [])
    tool_calls = list(final.get("tool_calls") or [])
    versions = dict(final.get("prompt_versions") or {})
    versions.setdefault(TASK_ID, rendered.version)
    cost = (sum(float(item.get("cost_cny") or 0) for item in records)
            + sum(float(item.get("cost_cny") or 0) for item in tool_calls))
    prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in (*records, *tool_calls))
    completion_tokens = sum(int(item.get("completion_tokens") or 0)
                            for item in (*records, *tool_calls))
    return ChatTurn(
        reply=str(final.get("reply") or ""),
        tool_calls=tool_calls,
        run_id=str(final.get("run_id") or ""),
        cost_cny=round(cost, 4),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        prompt_versions=versions,
    )

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

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from typing_extensions import TypedDict

from ..agents.supervisor import TASK_ID, initial_messages, supervise
from ..config import AppConfig
from ..core import budget as budget_tool
from ..core import ledger as ledger_projection
from ..core import progress as progress_tool
from ..core.budget import BudgetDecision, BudgetPolicy
from ..core.progress import error_category as error_category_of
from ..services import chat_tools
from ..services.chat_tools import ToolOutcome
from ..tools.llm import BaseProvider
from ..util import now_iso

EventFn = Callable[[str, dict[str, Any]], Awaitable[None]]
ToolRunner = Callable[..., Awaitable[ToolOutcome]]
#: 预算判定观察者（落 `context_assembled` 观测；不阻断图执行）
BudgetFn = Callable[[BudgetDecision], Awaitable[None]]

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
    # R3（OBS-07）：轮级用量完整性——按调用集合计算（actual / estimated 视为已知，
    # unknown 或"有调用无用量"计入 unknown 数），失败 / 取消轮次如实不完整
    usage_complete: bool = True
    usage_unknown_calls: int = 0


def _usage_accounting(
    records: list[dict[str, Any]], tool_calls: list[dict[str, Any]]
) -> tuple[bool, int]:
    """按调用集合计算轮级用量完整性（R3 / R5-E7OBS2-04）：单列未知调用数，不冒充准确。

    **来源优先**，数字不再参与"有没有调用"的判断（零值不因 truthiness 被改判）：
    - `actual` / `estimated`：计费调用且**已知**（供应商零用量、零单价也已知）；
    - `unknown`：调用发生或可能发生、无可靠用量 —— 未知 +1（已知总额仍保留）；
    - `cached`：缓存命中 / 本轮无新增消费 —— 不算调用，也不算未知（与免费真实请求区分）；
    - 来源缺失：有 tokens 或成本 = 未标来源的记录（旧数据 / 旁路），按未知 +1，不掩盖；
      全零 = 查库 / 读运行这类非计费调用，不参与核算。

    `usage_complete` 的语义 = "本轮的计费调用**均可解释**"（actual 与 estimated 都算可解释；
    是否全部为供应商实际用量是另一个维度，见 `usage_status`）。查库 / 读运行 / 缓存命中
    不参与核算。
    """
    unknown = 0
    for item in (*records, *tool_calls):
        status = str(item.get("usage_status") or "")
        if status in ("actual", "estimated", "cached"):
            continue
        if status == "unknown":
            unknown += 1
            continue
        # 来源缺失：有数字说明发生过消费但没有口径——按未知处理
        tokens = int(item.get("prompt_tokens") or 0) + int(
            item.get("completion_tokens") or 0
        )
        cost = float(item.get("cost_cny") or 0)
        if tokens > 0 or cost > 0:
            unknown += 1
    return unknown == 0, unknown


def _build_graph(
    *,
    provider: BaseProvider,
    run_tool: ToolRunner,
    max_rounds: int,
    on_event: EventFn | None,
    stream: bool,
    version: int,
    ledger: dict[str, Any],
    budget_policy: BudgetPolicy | None = None,
    on_budget: BudgetFn | None = None,
) -> Any:
    """按这一轮的实际依赖构图（provider / 工具执行器都是闭包注入，便于离线测试）。

    S7.26：`budget_policy.enforce` 时**每次 supervisor 调用前**做完整预算判定（含工具返回后的
    再检查）；超限抛 `BudgetError`（类别 `required_context_overflow`），不发请求。
    `shadow` 只记录判定，不改变执行。
    """
    policy = budget_policy or BudgetPolicy()
    # 调用身份注入（E7OBS4-01）：一次签名检查决定是否给 runner 传内部 `meta`；
    # 旧测试 runner 天然兼容，**不**用"捕获 TypeError 再重跑工具"来试探签名。
    try:
        meta_aware = "meta" in inspect.signature(run_tool).parameters
    except (TypeError, ValueError):  # 内建 / C 实现没有可查签名
        meta_aware = False

    async def emit(event: str, payload: dict[str, Any]) -> None:
        if on_event is not None:
            await on_event(event, payload)

    async def check_budget(messages: list[dict[str, Any]],
                           tools: list[dict[str, Any]]) -> BudgetDecision | None:
        """每次真实请求前的统一入口；返回判定（off 模式返回 None，不产生额外开销）。"""
        if not policy.checks:
            return None
        decision = budget_tool.decide(
            messages=messages,
            tools=tools,
            output_reserve=policy.output_tokens,
            application_limit=policy.application_limit,
            model_window=policy.model_window,
            mode=policy.mode,
            protected=policy.protected,
        )
        if on_budget is not None:
            await on_budget(decision)
        if policy.enforces and not decision.allowed:
            # 必要内容装不下就不发请求：让用户缩小范围，而不是静默截断约束
            raise budget_tool.BudgetError(
                "当前问题的必需上下文超出模型请求预算，请缩小范围后重试",
                reason="required_context_overflow",
            )
        return decision

    async def supervisor_node(state: ChatState) -> dict[str, Any]:
        rounds = int(state.get("tool_rounds") or 0)
        attempt = rounds + 1  # 第几轮决策（多轮决策不等于重试）
        remaining = max(0, int(max_rounds) - rounds)
        tools = chat_tools.TOOLS if remaining > 0 else []  # 到上限就不给工具，逼出正文
        messages = list(state.get("messages") or [])
        # S7.26：工具返回后、下一次调用前重新检查（mutation 不绕过预算入口）
        if policy.enforces:
            budget_tool.check_tool_structure(messages)
        await check_budget(messages, tools)

        # S7.20：决策阶段的生命周期事件——前端据此区分"理解与决策 / 工具 / 排队 / 回复"
        await emit(
            "stage",
            {
                "stage": "supervisor",
                "label": progress_tool.label_for("supervisor"),
                "status": "running",
                "attempt": attempt,
                "started_at": now_iso(),
                "updated_at": now_iso(),
            },
        )

        async def delta(piece: str) -> None:
            await emit("text_delta", {"text": piece})

        try:
            turn = await supervise(
                provider,
                messages,
                tools=tools,
                on_delta=delta if stream else None,
                stream=stream,
            )
        except asyncio.CancelledError:
            # R5（E7OBS2-07）：取消**不是失败**——当前执行如实投影 interrupted/cancelled，
            # 与统一收尾（消息 interrupted / 类别 cancelled）和前端中断提示保持一致；
            # 既有历史失败节点不受影响；尽力广播后继续传播取消控制流
            with suppress(asyncio.CancelledError):
                await emit(
                    "stage",
                    {
                        "stage": "supervisor",
                        "status": "interrupted",
                        "attempt": attempt,
                        "error_category": "cancelled",
                        "updated_at": now_iso(),
                    },
                )
            raise
        except BaseException as exc:
            await emit(
                "stage",
                {
                    "stage": "supervisor",
                    "status": "failed",
                    "attempt": attempt,
                    "error_category": error_category_of(exc),
                    "updated_at": now_iso(),
                },
            )
            raise
        await emit(
            "stage",
            {
                "stage": "supervisor",
                "status": "succeeded",
                "attempt": attempt,
                "updated_at": now_iso(),
            },
        )
        records = [*(state.get("records") or []), turn.record]
        # R3（OBS-10）：稳定边界账目——图异常时已完成的调用记录仍能带出（状态可能拿不到）
        ledger["records"].append(turn.record)
        # 已知 / 未知**按工具清单动态划分**（S7.7a）：这里不写任何具体工具名，
        # 加工具只改 `services/chat_tools.py` 一处。清单读模块属性而不是导入时快照，
        # 用例注册一个假工具也能被认出来。
        registered = chat_tools.tool_names()
        known = [call for call in turn.tool_calls if call["name"] in registered]
        unknown = [call for call in turn.tool_calls if call["name"] not in registered]

        history = list(state.get("tool_calls") or [])
        for call in unknown:
            history.append(
                {
                    "tool": call["name"],
                    "status": "failed",
                    "args": call["args"],
                    "run_id": None,
                    "cost_cny": 0.0,
                    "latency_ms": 0,
                    "error": "模型要调一个不存在的工具",
                    "steps": [],
                }
            )
        reply = turn.text or str(state.get("reply") or "")
        pending = known if remaining > 0 else []
        if not pending and not reply:
            reply = (
                "这条消息我一时判断不了要怎么接，你能说清楚一点吗——想蹭哪条热点，"
                "或者想让我做哪一步？"
            )
        return {
            "records": records,
            "version": version,
            "reply": reply,
            "tool_calls": history,
            "pending": pending,
        }

    tool_attempts: dict[str, int] = {}  # 同一工具在本轮内的第几次执行（S7.20）

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
            await emit(
                "tool_started",
                {
                    "tool": name,
                    "args": call["args"],
                    "call_id": call_id,
                    "attempt": attempt,
                    "started_at": now_iso(),
                },
            )
            started = time.monotonic()
            # 工具最后一次上报的阶段键（S7.21）：`tool_finished` 用它把快照里的阶段收成终态
            # R5（E7OBS2-03）：同时记 **worker 来源身份**（最后结构化快照的尝试号 / 重试 /
            # 状态 / 是否结构化）——tool_finished 的"工具第几次调用"不是 worker 尝试号，
            # 绝不能用它覆盖阶段的重试号与真实状态。
            stage_holder: dict[str, Any] = {
                "key": "",
                "structured": False,
                "attempt": 0,
                "retries": 0,
                "status": "",
                "latency": 0,
                "source_id": "",
                "stage_instance_id": "",
                "source_seq": 0,
                "progress_revision": 0,
                "generation": 0,
            }
            # E7OBS4-01：调用开始即登记真实身份与参数——取消 / 失败时由**同一条记录**
            # 派生部分终态，不靠事后补字段；内部 `scope` 用 call_id（单次调用唯一）。
            draft: dict[str, Any] = {
                "tool": name,
                "call_id": call_id,
                "attempt": attempt,
                "args": dict(call["args"]) if isinstance(call["args"], dict) else {},
                "scope": call_id,
                "stage_key": "",
                "run_id": "",
                "latency_ms": 0,
                "steps": [],
            }
            ledger.setdefault("calls", {})[call_id] = draft

            async def progress(
                stage: str,
                done: int = 0,
                total: int = 0,
                *,
                update: dict[str, Any] | None = None,
                _call: dict[str, Any] = call,
                _call_id: str = call_id,
                _attempt: int = attempt,
                _holder: dict[str, Any] = stage_holder,
                _draft: dict[str, Any] = draft,
                _started: float = started,
            ) -> None:
                """工具进度 → `tool_progress` 事件（S7.20 / R3 / R5）。

                `update` 是 worker `runs.stage` 快照的结构化投影：阶段键 / **真实状态** /
                **源尝试号** / 重试次数 / 错误类别 / run_id / job_id / 终态耗时。工具第几次
                调用（`tool_attempt`）与 worker 第几次尝试（`attempt`）**分层表达**，不互相
                代用；没有 `update` 时退回工具内部的标签进度（如「解析图片」）。
                """
                data = dict(update or {})
                key = str(data.get("stage_key") or progress_tool.key_for_label(stage))
                if key:
                    _holder["key"] = key
                    _draft["stage_key"] = key
                # 部分记录要能说明"真实发生过的步骤"：按标签变化收集，未拿到的步骤不补造
                _draft["latency_ms"] = int((time.monotonic() - _started) * 1000)
                label = str(data.get("label") or stage)
                steps = _draft.setdefault("steps", [])
                if label and (not steps or steps[-1].get("stage") != label):
                    steps.append({"stage": label, "done": done, "total": total})
                if data:  # 结构化来源：记下 worker 身份供收尾对齐
                    _holder["structured"] = True
                    _holder["attempt"] = int(data.get("attempt") or 0)
                    _holder["retries"] = int(data.get("retries") or 0)
                    _holder["status"] = str(data.get("status") or "")
                    _holder["latency"] = int(data.get("latency_ms") or 0)
                    _holder["source_id"] = str(data.get("source_id") or "")
                    _holder["stage_instance_id"] = str(
                        data.get("stage_instance_id") or ""
                    )
                    _holder["source_seq"] = int(data.get("source_seq") or 0)
                    _holder["progress_revision"] = int(
                        data.get("progress_revision") or 0
                    )
                    _holder["generation"] = int(data.get("generation") or 0)
                body: dict[str, Any] = {
                    "tool": _call["name"],
                    "stage": str(data.get("label") or stage),
                    "stage_key": key,
                    "status": str(data.get("status") or "running"),
                    "done": done,
                    "total": total,
                    "call_id": _call_id,
                    "attempt": int(data.get("attempt") or _attempt),
                    "tool_attempt": _attempt,
                    "retries": int(data.get("retries") or 0),
                    "error_category": str(data.get("error_category") or ""),
                    # R5（E7OBS2-03）：结构化终态耗时原样透传——此前 body 漏了这个字段，
                    # 对话侧持久化的节点耗时恒为 0（端到端断链）。
                    "latency_ms": int(data.get("latency_ms") or 0),
                    "run_id": data.get("run_id") or None,
                    "job_id": data.get("job_id") or None,
                    "source_id": data.get("source_id") or None,
                    "stage_instance_id": data.get("stage_instance_id") or None,
                    "source_seq": int(data.get("source_seq") or 0),
                    "progress_revision": int(data.get("progress_revision") or 0),
                    "generation": int(data.get("generation") or 0),
                    "updated_at": now_iso(),
                }
                await emit("tool_progress", body)

            if call.get("args_error"):
                outcome = ToolOutcome(status="failed", error=str(call["args_error"]))
            elif meta_aware:
                outcome = await run_tool(
                    call["name"],
                    call["args"],
                    on_progress=progress,
                    meta={"call_id": call_id, "attempt": attempt, "scope": call_id},
                )
            else:
                outcome = await run_tool(
                    call["name"], call["args"], on_progress=progress
                )

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
                # R3（OBS-07）：用量口径（actual / estimated / unknown）随调用上卷
                "usage_status": str(outcome.payload.get("usage_status") or ""),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": outcome.error,
                "steps": outcome.steps,
                # E7 第六批（S7.25）：改稿的版本链随调用记录落库（前端显示成功版本 / 基础版本）
                "object_key": str(outcome.payload.get("object_key") or ""),
                "version_id": outcome.payload.get("version_id"),
                "version_no": outcome.payload.get("version_no"),
                "base_version_no": outcome.payload.get("base_version_no"),
            }
            history.append(entry)
            # R3（OBS-10）：稳定边界账目——工具调用记录带已知用量与身份
            # E7OBS4-02：调用草稿在这里"结清"；投影只从已完成记录取一次账目。
            ledger.setdefault("calls", {}).pop(call_id, None)
            ledger["tool_calls"].append(dict(entry))
            ledger["versions"].update(outcome.prompt_versions)
            # 工具内部用掉的 prompt 版本（拆解 / 改写…）一并计入这一轮
            versions.update(outcome.prompt_versions)
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": call["arguments"],
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    # S7.26：enforce 时超长工具结果按字段限量（保留对象 ID / 版本 / 可读引用）；
                    # shadow 不改工具执行，off 保持原样
                    "content": (
                        budget_tool.compact_tool_message(outcome.as_tool_message())
                        if policy.enforces
                        else outcome.as_tool_message()
                    ),
                }
            )
            # R5（E7OBS2-03）：worker 来源事实（结构化标记 / 源尝试号 / 重试 / 最后状态）
            # 随 tool_finished 一并上报——消费侧据此对齐，不用工具次数覆盖阶段重试号
            await emit(
                "tool_finished",
                {
                    **entry,
                    "ended_at": now_iso(),
                    "structured": bool(stage_holder["structured"]),
                    "worker_attempt": int(stage_holder["attempt"]),
                    "worker_retries": int(stage_holder["retries"]),
                    "worker_status": str(stage_holder["status"]),
                    "worker_latency": int(stage_holder["latency"]),
                    "source_id": stage_holder["source_id"] or None,
                    "stage_instance_id": stage_holder["stage_instance_id"] or None,
                    "source_seq": int(stage_holder["source_seq"]),
                    "progress_revision": int(stage_holder["progress_revision"]),
                    "generation": int(stage_holder["generation"]),
                },
            )
            run_id = str(outcome.payload.get("run_id") or run_id)
            if run_id:
                ledger["run_id"] = run_id

        return {
            "messages": messages,
            "tool_calls": history,
            "pending": [],
            "prompt_versions": versions,
            "tool_rounds": rounds + len(state.get("pending") or []),
            "run_id": run_id,
        }

    def route(state: ChatState) -> str:
        return "tools" if state.get("pending") else "end"

    graph = StateGraph(ChatState)
    graph.add_node("supervisor", supervisor_node, retry_policy=RETRY_POLICY)
    graph.add_node("tools", tools_node, retry_policy=RETRY_POLICY)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", route, {"tools": "tools", "end": END})
    graph.add_edge("tools", "supervisor")
    return graph.compile()


async def run_turn(
    cfg: AppConfig,
    *,
    provider: BaseProvider,
    history: list[dict[str, Any]],
    user_text: str,
    run_tool: ToolRunner,
    attachment_note: str = "",
    max_rounds: int | None = None,
    on_event: EventFn | None = None,
    stream: bool = True,
    ledger: dict[str, Any] | None = None,
    state_text: str = "",
    grouped: bool = False,
    budget_policy: BudgetPolicy | None = None,
    on_budget: BudgetFn | None = None,
) -> ChatTurn:
    """跑一轮对话：组装消息 → 图（supervisor ⇄ 工具）→ 汇总成 `ChatTurn`。

    `ledger` 可注入（R5 / E7OBS2-01）：对话运行器在**稳定边界**持有同一份账本，取消 /
    外层异常收尾时也能读出已完成的模型与工具记录（已知消费不丢，未知单列）。

    S7.24 / S7.26：`state_text` 是服务端验证过的有效状态（放固定前缀之后）；`grouped`
    启用完整轮次组裁剪；`budget_policy` 非 off 时每次实际请求前做完整预算判定。
    """
    started = time.monotonic()
    rounds = int(max_rounds if max_rounds is not None else cfg.chat.max_tool_rounds)
    rendered, messages = initial_messages(
        history,
        user_text,
        attachment_note=attachment_note,
        keep_messages=cfg.chat.history_max_messages,
        token_budget=cfg.chat.history_token_budget,
        grouped=grouped,
        state_text=state_text,
    )
    # R3（OBS-10）：轮内账目挂在**稳定边界**（闭包持有的 ledger）——图抛异常时，
    # 已完成的模型 / 工具调用记录仍能带出，失败轮次不丢已知消费与引用
    if ledger is None:
        ledger = {
            "records": [],
            "calls": {},
            "tool_calls": [],
            "components": {},
            "versions": {},
            "run_id": "",
        }
    # E7CTX-08：注入的账本**只使用不清空**——轮次入口在抽取前初始化账本，
    # 抽取的消费记录必须带进对话图（清空会让抽取费用漏计）。串轮隔离由调用方负责。
    graph = _build_graph(
        provider=provider,
        run_tool=run_tool,
        max_rounds=rounds,
        on_event=on_event,
        stream=stream,
        version=rendered.version,
        ledger=ledger,
        budget_policy=budget_policy,
        on_budget=on_budget,
    )
    try:
        final = await graph.ainvoke(
            {
                "messages": messages,
                "reply": "",
                "pending": [],
                "tool_calls": [],
                "records": [],
                "run_id": "",
                "tool_rounds": 0,
                "version": rendered.version,
                # 这一轮的 prompt 版本先记上 supervisor 自己，工具内部用掉的由 tools_node 合并进来
                "prompt_versions": {TASK_ID: rendered.version},
            }
        )
    except Exception as exc:  # noqa: BLE001 - 整轮失败要落库，不能炸进程
        # E7OBS4-02：普通异常返回与取消 / 外层异常共用**同一份账目投影**——
        # 工具内部已完成组件的消费（如"图片成功、等待分析时报错"）在这条路径同样保留。
        projected = ledger_projection.project(ledger, open_status="failed")
        records = list(ledger["records"])
        tool_calls = list(projected["tool_calls"])
        versions = dict(projected["prompt_versions"])
        versions.setdefault(TASK_ID, rendered.version)
        usage_complete, unknown = _usage_accounting(records, tool_calls)
        if not projected["open_calls"]:
            # 没有未完成的工具调用时，失败的是模型那次调用本身：用量未知，单列一次
            unknown += 1
        return ChatTurn(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            error_category=error_category_of(exc),
            latency_ms=int((time.monotonic() - started) * 1000),
            tool_calls=tool_calls,
            run_id=str(projected["run_id"]),
            cost_cny=float(projected["cost_cny"]),
            prompt_tokens=int(projected["prompt_tokens"]),
            completion_tokens=int(projected["completion_tokens"]),
            prompt_versions=versions,
            usage_complete=False,
            usage_unknown_calls=unknown,
        )

    records = list(final.get("records") or [])
    # 展示用工具列表保留图里全部已完成条目（含模型点名不存在的工具等零消费记录）；
    # 账目与"部分记录"来自统一投影，保证四条收尾路径口径一致。
    tool_calls = list(final.get("tool_calls") or [])
    projected = ledger_projection.project(ledger)
    versions = dict(final.get("prompt_versions") or {})
    versions.setdefault(TASK_ID, rendered.version)
    cost = float(projected["cost_cny"])
    prompt_tokens = int(projected["prompt_tokens"])
    completion_tokens = int(projected["completion_tokens"])
    usage_complete, unknown = _usage_accounting(records, projected["tool_calls"])
    return ChatTurn(
        reply=str(final.get("reply") or ""),
        tool_calls=tool_calls,
        run_id=str(final.get("run_id") or projected["run_id"] or ""),
        usage_complete=usage_complete,
        usage_unknown_calls=unknown,
        cost_cny=round(cost, 4),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=int((time.monotonic() - started) * 1000),
        prompt_versions=versions,
    )

"""完整请求预算与成组裁剪（S7.26）。

用途：在**每次实际模型请求前**算一次完整预算——system / tools / 状态 / 历史 / 本轮问题 /
工具往返 / 协议开销全部计入，输出预留取该次真实 `max_tokens`，再加安全余量。
输入：模型可见消息（`messages`）、工具清单、输出预留、应用上限与（已核实的）模型窗口。
输出：`BudgetDecision`（是否放行 + 各部分估算 + 裁剪结果 + 原因）。

口径（与方案 §7 一致）：

```text
input_est = system + tools + state + history + current + tool_round_trips + protocol_overhead
effective_limit = min(application_limit, verified_model_context_window)
safety = max(512, ceil(input_est × 10%))
allow = input_est + actual_max_tokens + safety <= effective_limit
```

- **估算器**：沿用 `tools/llm.py::estimate_tokens` 的「1.5 字/token」，但本模块不 import
  `tools`（保持 core 不反向依赖适配器），公式在两处各自实现，由单测钉住一致性；
- **成组裁剪**：先按 `user` 消息切轮次，再保留预算内的**连续后缀**；遇到最近一个完整组超限就
  停下，**不跳过它去找更早的短消息**；当前问题、有效状态与必需基础版不参与裁剪（调用方作为
  `required` 单独计入，超限即判定不放行）；
- **结构校验**：`assistant(tool_calls)` 必须紧跟同 id 的 `tool` 结果，孤立结果 / 缺结果都算
  `history_group_broken`，不静默发送；
- **工具结果限量**：超长 tool 消息按字段裁剪（保留对象 ID、版本与可读引用），并标 `truncated`。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

#: 估算器标识：随每次预算事件落库，便于事后对比估算与实际偏差
ESTIMATOR = "chars_div_1.5_v1"
#: 协议开销下限（每条消息的角色 / 分隔符等）
PROTOCOL_OVERHEAD_MIN = 32
PROTOCOL_OVERHEAD_PER_MESSAGE = 8
#: 安全余量下限（tokens）
SAFETY_MIN = 512
#: 单条 tool 消息的字符上限：超过就按字段裁剪（约 1300 tokens）
TOOL_MESSAGE_CHAR_LIMIT = 2000
#: 裁剪工具结果时**永不丢弃**的字段：对象标识、版本与可实际读取的引用
PROTECTED_TOOL_KEYS = (
    "run_id",
    "job_id",
    "version_id",
    "version_no",
    "base_version_id",
    "object_key",
    "status",
    "hotspot_index",
    "hotspot_id",
    "material_id",
    "message_id",
    "url",
    "refused",
    "reason",
)
#: 单条消息的角色集合（模型可见）
ROLES = ("system", "user", "assistant", "tool")


class BudgetError(ValueError):
    """预算判定失败（必要内容超限 / 历史结构损坏）。"""

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(message)


def estimate_tokens(text: str) -> int:
    """与 `tools/llm.py::estimate_tokens` 同一公式（中文约 1.5 字/token）。"""
    return max(1, int(len(str(text or "")) / 1.5))


def estimate_messages(messages: Iterable[dict[str, Any]]) -> int:
    """一组消息的估算：正文 + 工具调用参数 + 每条协议开销。"""
    total = 0
    for message in messages:
        total += estimate_tokens(_message_text(message))
        total += PROTOCOL_OVERHEAD_PER_MESSAGE
    return max(0, total)


def estimate_tools(tools: Sequence[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return estimate_tokens(json.dumps(list(tools), ensure_ascii=False, sort_keys=True))


def protocol_overhead(message_count: int) -> int:
    return max(PROTOCOL_OVERHEAD_MIN, message_count * PROTOCOL_OVERHEAD_PER_MESSAGE)


def _message_text(message: dict[str, Any]) -> str:
    """一条消息参与估算的文本：正文 + `tool_calls` 的 name/arguments。"""
    content = message.get("content")
    text = content if isinstance(content, str) else json.dumps(content or "", ensure_ascii=False)
    calls = message.get("tool_calls") or []
    if calls:
        parts = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            raw_function = call.get("function")
            function: dict[str, Any] = (
                raw_function if isinstance(raw_function, dict) else {}
            )
            parts.append(f"{function.get('name', '')}{function.get('arguments', '')}")
        text = f"{text}\n{' '.join(parts)}"
    return text


# ---------------------------------------------------------------------------
# 轮次分组与结构校验
# ---------------------------------------------------------------------------


def group_turns(messages: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """按 `user` 消息切成"完整轮次组"；开头的非 user 消息自成一组。

    这样裁剪时只会整组取舍，不会把一轮里的提问与回答拆开（tool 往返同属一组）。
    """
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "user" and current:
            groups.append(current)
            current = [message]
            continue
        current.append(message)
    if current:
        groups.append(current)
    return groups


def validate_tool_pairs(messages: Sequence[dict[str, Any]]) -> list[str]:
    """校验工具调用与结果的配对；返回问题清单（空 = 结构完整）。

    - assistant 的每个 `tool_calls[].id` 必须在本组内出现同 id 的 `tool` 结果；
    - `tool` 消息的 `tool_call_id` 必须有对应的 assistant 调用；
    - 不跨组配对（组边界即轮次边界）。
    """
    problems: list[str] = []
    for index, group in enumerate(group_turns(messages)):
        pending: dict[str, int] = {}
        seen_results: set[str] = set()
        for message in group:
            role = str(message.get("role") or "")
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = str((call or {}).get("id") or "")
                    if call_id:
                        pending[call_id] = pending.get(call_id, 0) + 1
            elif role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if not call_id:
                    problems.append(f"第 {index + 1} 组有 tool 消息缺少 tool_call_id")
                    continue
                if pending.get(call_id, 0) <= 0:
                    problems.append(f"第 {index + 1} 组有孤立的 tool 结果（{call_id}）")
                    continue
                pending[call_id] -= 1
                seen_results.add(call_id)
        leftovers = [key for key, count in pending.items() if count > 0]
        for call_id in leftovers:
            problems.append(f"第 {index + 1} 组有工具调用没有结果（{call_id}）")
    return problems


def fit_history(
    messages: Sequence[dict[str, Any]], *, max_tokens: int
) -> tuple[list[dict[str, Any]], int, int]:
    """保留预算内的**连续完整轮次后缀**，返回（保留的消息、丢掉的组数、保留的组数）。

    从最新一组往回加，遇到装不下的组就停止——不跳过它去找更早的短消息（方案 §7.2）。
    """
    groups = group_turns(messages)
    kept: list[list[dict[str, Any]]] = []
    used = 0
    for group in reversed(groups):
        size = estimate_messages(group)
        if kept and used + size > max_tokens:
            break
        if not kept and size > max_tokens:
            # 最新一组自己就超限：一个都不保留，交给调用方判断（当前问题另有保护）
            break
        kept.append(group)
        used += size
    ordered = [message for group in reversed(kept) for message in group]
    return ordered, len(groups) - len(kept), len(kept)


# ---------------------------------------------------------------------------
# 工具结果限量
# ---------------------------------------------------------------------------


def compact_tool_message(text: str, *, limit: int = TOOL_MESSAGE_CHAR_LIMIT) -> str:
    """超长 tool 消息按字段裁剪：保留受保护字段，列表截断并标 `truncated`。

    不是合法 JSON 时按字符截断并标 `truncated`（不回退成空串——模型至少能看到开头）。
    返回的 JSON 里 `truncated=true` 与 `truncated_fields` 说明裁了什么，引用仍可读取。
    """
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return json.dumps(
            {"truncated": True, "preview": raw[: max(0, limit - 64)]}, ensure_ascii=False
        )
    if not isinstance(body, dict):
        return json.dumps(
            {"truncated": True, "preview": raw[: max(0, limit - 64)]}, ensure_ascii=False
        )
    compacted, dropped = _compact_value(body, keep=list(PROTECTED_TOOL_KEYS), depth=0)
    result: dict[str, Any] = dict(compacted) if isinstance(compacted, dict) else {}
    result["truncated"] = True
    if dropped:
        result["truncated_fields"] = dropped[:20]
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded
    # 仍然过长：只留受保护字段 + 一句说明（引用仍可读取）
    minimal = {key: body[key] for key in PROTECTED_TOOL_KEYS if key in body}
    minimal["truncated"] = True
    minimal["truncated_fields"] = dropped[:20] or ["*"]
    return json.dumps(minimal, ensure_ascii=False)


def _compact_value(value: Any, *, keep: list[str], depth: int) -> tuple[Any, list[str]]:
    """递归裁剪：字典保留受保护键，列表截断到前 3 项，长字符串截到 200 字。"""
    dropped: list[str] = []
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in keep:
                out[key] = item
                continue
            compacted, sub = _compact_value(item, keep=keep, depth=depth + 1)
            out[key] = compacted
            dropped.extend(sub)
            if depth >= 2:
                dropped.append(str(key))
        return out, dropped
    if isinstance(value, list):
        out_list = []
        for index, item in enumerate(value):
            if index >= 3:
                dropped.append(f"[{index}]")
                continue
            compacted, sub = _compact_value(item, keep=keep, depth=depth + 1)
            out_list.append(compacted)
            dropped.extend(sub)
        return out_list, dropped
    if isinstance(value, str) and len(value) > 200:
        dropped.append("long_text")
        return value[:200] + "…", dropped
    return value, dropped


# ---------------------------------------------------------------------------
# 预算判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetDecision:
    """一次请求前的预算判定（可直接落 `context_assembled` 观测）。"""

    allowed: bool
    input_est: int
    output_reserve: int
    safety: int
    effective_limit: int
    estimator: str
    parts: dict[str, int] = field(default_factory=dict)
    reason: str = ""
    dropped_turns: int = 0
    kept_groups: int = 0
    protected: tuple[str, ...] = ()
    mode: str = "off"
    history_messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.input_est + self.output_reserve + self.safety

    def as_event(self) -> dict[str, Any]:
        """观测 payload：只放数字与原因，不放正文。"""
        return {
            "allowed": self.allowed,
            "input_est": self.input_est,
            "output_reserve": self.output_reserve,
            "safety": self.safety,
            "effective_limit": self.effective_limit,
            "total": self.total,
            "estimator": self.estimator,
            "parts": dict(self.parts),
            "reason": self.reason,
            "dropped_turns": self.dropped_turns,
            "kept_groups": self.kept_groups,
            "protected": list(self.protected),
            "mode": self.mode,
        }


def safety_margin(input_est: int) -> int:
    return max(SAFETY_MIN, math.ceil(input_est * 0.1))


def decide(
    *,
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    output_reserve: int,
    application_limit: int,
    model_window: int | None = None,
    mode: str = "enforce",
    protected: Sequence[str] = (),
) -> BudgetDecision:
    """对**已经装配好**的消息序列做一次完整预算判定（不裁剪，只判定）。

    `messages` 里 system 与历史 / 本轮都在其中；`parts` 按角色拆分，便于事后定位是哪一段超的。
    """
    system = sum(
        estimate_tokens(_message_text(item))
        for item in messages
        if str(item.get("role")) == "system"
    )
    history = sum(
        estimate_tokens(_message_text(item))
        for item in messages
        if str(item.get("role")) in ("user", "assistant", "tool")
    )
    tool_tokens = estimate_tools(tools)
    overhead = protocol_overhead(len(messages))
    input_est = system + history + tool_tokens + overhead
    limit = int(application_limit)
    if model_window:
        limit = min(limit, int(model_window))
    reserve = max(0, int(output_reserve))
    safety = safety_margin(input_est)
    total = input_est + reserve + safety
    allowed = total <= limit
    reason = "" if allowed else "required_context_overflow"
    return BudgetDecision(
        allowed=allowed,
        input_est=input_est,
        output_reserve=reserve,
        safety=safety,
        effective_limit=limit,
        estimator=ESTIMATOR,
        parts={
            "system": system,
            "tools": tool_tokens,
            "history": history,
            "protocol_overhead": overhead,
        },
        reason=reason,
        protected=tuple(protected),
        mode=mode,
    )


def decide_with_history(
    *,
    system_messages: Sequence[dict[str, Any]],
    history: Sequence[dict[str, Any]],
    current: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    output_reserve: int,
    application_limit: int,
    history_budget: int,
    model_window: int | None = None,
    mode: str = "enforce",
    protected: Sequence[str] = (),
) -> BudgetDecision:
    """首轮装配：先按 `history_budget` 裁历史，再做整请求判定。

    状态是 `system_messages` 的一部分（动态状态放在固定前缀之后），因此不会被历史裁剪丢掉。
    """
    kept, dropped, kept_groups = fit_history(history, max_tokens=max(0, int(history_budget)))
    messages = [*system_messages, *kept, *current]
    decision = decide(
        messages=messages,
        tools=tools,
        output_reserve=output_reserve,
        application_limit=application_limit,
        model_window=model_window,
        mode=mode,
        protected=protected,
    )
    return replace(
        decision,
        dropped_turns=dropped,
        kept_groups=kept_groups,
        history_messages=list(kept),
    )


def check_tool_structure(messages: Sequence[dict[str, Any]]) -> None:
    """发送前结构校验：有问题抛 `BudgetError(history_group_broken)`。"""
    problems = validate_tool_pairs(messages)
    if problems:
        raise BudgetError("；".join(problems[:5]), reason="history_group_broken")


@dataclass(frozen=True)
class BudgetPolicy:
    """一次轮次内的预算策略（由调用方从 `[context]` / `[llm]` 组装，不在 core 里读配置）。"""

    application_limit: int = 65536
    model_window: int | None = None
    mode: str = "off"
    output_tokens: int = 16000
    protected: tuple[str, ...] = ()

    @property
    def checks(self) -> bool:
        """是否执行预算检查（off 模式不做，保持旧行为）。"""
        return self.mode in ("shadow", "enforce")

    @property
    def enforces(self) -> bool:
        return self.mode == "enforce"

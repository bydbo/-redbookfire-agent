"""对话层 supervisor（S7.3）：一次带工具的模型调用。

用途：把「组装多轮消息 → 带 tools 调模型（可真流式）→ 归一化出正文与 tool_calls」包成一步，
      供 `workflows/chat.py` 的图节点调用。
输入：`BaseProvider` + **完整的模型可见消息列表**（首轮由 `initial_messages` 组装，之后由
      对话图把 tool 往返追加进去）+ 工具清单；`on_delta` 可选。
输出：`SupervisorOutcome`（text / tool_calls / record / version）。

口径：

- prompt 资产 `chat_supervisor` 的五段照旧组装：**system = 01+03**、**user = 02+04+05**；
  后者的渲染结果作为对话里的第一条 user 消息（"任务说明"），之后才是真实对话——
  这样五段结构不变，动态内容全走消息而不是模板占位符（`requires` 为空）；
- `tool_calls` 里的 `arguments` 是原始 JSON 字符串；这里顺手解析成 `args`，解析失败不抛错，
  而是把 `args_error` 带上——由工具节点返回一句"参数没看懂"给模型，而不是整轮崩掉。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..tools import prompt as prompt_tool
from ..tools.llm import BaseProvider, LLMCall, LLMError

TASK_ID = "chat_supervisor"

DeltaFn = Callable[[str], Awaitable[None]]


@dataclass
class SupervisorTurn:
    """一次 supervisor 调用的结果。"""

    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tool(self) -> bool:
        return bool(self.tool_calls)


def initial_messages(history: list[dict[str, Any]], user_text: str, *,
                     attachment_note: str = "") -> tuple[prompt_tool.RenderedPrompt,
                                                        list[dict[str, Any]]]:
    """首轮消息：渲染 prompt（拿版本号）+ 组装 system / 任务说明 / 历史 / 本轮用户消息。"""
    rendered = prompt_tool.render(TASK_ID)
    return rendered, build_messages(rendered, history, user_text,
                                    attachment_note=attachment_note)


def build_messages(rendered: prompt_tool.RenderedPrompt, history: list[dict[str, Any]],
                   user_text: str, *, attachment_note: str = "") -> list[dict[str, Any]]:
    """组装给模型的消息序列：system 指令 → 任务说明 → 历史 → 本轮用户消息。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": rendered.system},
        {"role": "user", "content": rendered.user},
    ]
    for item in history:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue          # 正在生成中的 assistant 行（content 为空）不进上下文
        messages.append({"role": role, "content": content})
    current = user_text.strip() or "（这条消息没有文字，只有附件）"
    if attachment_note:
        current = f"{current}\n\n[附件] {attachment_note}"
    messages.append({"role": "user", "content": current})
    return messages


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str]:
    """解析工具参数 JSON；失败时返回 `({}, 原因)`（不抛错，交给工具节点回话）。"""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"参数不是合法 JSON（{exc.msg}）"
    if not isinstance(parsed, dict):
        return {}, "参数必须是 JSON 对象"
    return parsed, ""


def normalise_tool_calls(raw_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 provider 给的 `[{id, name, arguments}]` 归一成内部形状（补 `args` 与 `args_error`）。"""
    calls: list[dict[str, Any]] = []
    for raw in raw_calls:
        args, args_error = _parse_arguments(str(raw.get("arguments") or ""))
        calls.append({
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or ""),
            "arguments": str(raw.get("arguments") or ""),
            "args": args,
            "args_error": args_error,
        })
    return calls


async def supervise(provider: BaseProvider, messages: list[dict[str, Any]], *,
                    tools: list[dict[str, Any]] | None = None,
                    on_delta: DeltaFn | None = None,
                    stream: bool = True) -> SupervisorTurn:
    """跑一次 supervisor 调用（`messages` 是完整的模型可见对话）；返回正文与工具调用。"""
    result = await provider.complete_with_tools(
        LLMCall(task=TASK_ID, system="", user="", messages=list(messages),
                tools=list(tools) if tools else None, json_mode=False, stream=stream),
        on_delta=on_delta,
    )
    if result.error:
        raise LLMError(f"[{TASK_ID}] {result.error}")
    return SupervisorTurn(
        text=result.text,
        tool_calls=normalise_tool_calls(result.tool_calls),
        record=result.record(TASK_ID),
    )

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
- **历史按 token 预算装配（S7.10）**：最近的 `keep` 条保留原文，更早的降级成摘要（正文前
  60 字 + 一行工具结果摘要），累计超预算就裁剪并如实说明丢了几条——不硬截断、不调模型；
  历史里的工具摘要行带 `run_id`，模型因此能自己把 id 传回 `read_run` / `revise_draft`；
- **能力清单（S7.11）**：`prompts/chat_capabilities.json` 渲染进 §02 Task 的
  `{{ capabilities }}`，三类边界与固定话术只有这一份来源（ADR 0016）；
- `tool_calls` 里的 `arguments` 是原始 JSON 字符串；这里顺手解析成 `args`，解析失败不抛错，
  而是把 `args_error` 带上——由工具节点返回一句"参数没看懂"给模型，而不是整轮崩掉。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..tools import prompt as prompt_tool
from ..tools.llm import BaseProvider, LLMCall, LLMError, estimate_tokens

TASK_ID = "chat_supervisor"

DeltaFn = Callable[[str], Awaitable[None]]
# 历史装配的默认值（与配置默认一致；运行路径一律显式传 `[chat]` 段的值）
DEFAULT_KEEP_MESSAGES = 20
DEFAULT_TOKEN_BUDGET = 6000
DIGEST_CHARS = 60
TOOL_LINE_PREFIX = "[上一步] "


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
                     attachment_note: str = "",
                     keep_messages: int = DEFAULT_KEEP_MESSAGES,
                     token_budget: int = DEFAULT_TOKEN_BUDGET,
                     ) -> tuple[prompt_tool.RenderedPrompt, list[dict[str, Any]]]:
    """首轮消息：渲染 prompt（拿版本号）+ 组装 system / 任务说明 / 历史 / 本轮用户消息。"""
    rendered = prompt_tool.render(TASK_ID,
                                  capabilities=prompt_tool.render_capabilities())
    return rendered, build_messages(rendered, history, user_text,
                                    attachment_note=attachment_note,
                                    keep_messages=keep_messages,
                                    token_budget=token_budget)


def _clip(text: Any, limit: int) -> str:
    """折成单行并截断（摘要用）。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def tool_summary_line(tool_calls: list[dict[str, Any]] | None) -> str:
    """一次 assistant 消息里的工具调用 → 一行确定性摘要（带 `run_id`）。"""
    parts: list[str] = []
    for call in tool_calls or []:
        name = str(call.get("tool") or "").strip()
        if not name:
            continue
        status = str(call.get("status") or "succeeded")
        if status == "failed":
            reason = _clip(call.get("error") or "未说明原因", 40)
            parts.append(f"{name}（失败：{reason}）")
            continue
        run_id = str(call.get("run_id") or "").strip()
        parts.append(f"{name}（{status}）" + (f" run_id={run_id}" if run_id else ""))
    return f"{TOOL_LINE_PREFIX}{'；'.join(parts)}" if parts else ""


def _entry_text(item: dict[str, Any], *, digest: bool) -> str:
    """一条历史消息的文本：正文（摘要模式下截断）+ 工具结果摘要行。"""
    content = str(item.get("content") or "").strip()
    text = _clip(content, DIGEST_CHARS) if digest else content
    line = tool_summary_line(item.get("tool_calls"))
    return "\n".join(piece for piece in (text, line) if piece)


def _earlier_image_note(history: list[dict[str, Any]]) -> str:
    """历史图片提示：总张数 + **最近一组**的名字（最多列 3 个；S7.10 / S7.16）。

    只报最近一组的名字：9 张 × 多轮会把这一行撑爆，而模型只需要知道「早前有图可用、最近那组
    叫什么」。**整组**这一点很重要——回落时给工具的是那一整组，不是第一张。
    """
    total = 0
    latest: list[str] = []
    for item in history:
        names = [str(attachment.get("name") or "").strip()
                 for attachment in (item.get("attachments") or [])]
        names = [name for name in names if name]
        if not names:
            continue
        total += len(names)
        latest = names          # 越晚的消息覆盖它 → 循环结束就是最近一组
    if not latest:
        return ""
    listed = "、".join(latest[:3])
    suffix = f" 等 {len(latest)} 张" if len(latest) > 3 else ""
    return f"本会话早前传过 {total} 张图（最近一组：{listed}{suffix}）"


def assemble_history(history: list[dict[str, Any]], *,
                     keep_messages: int = DEFAULT_KEEP_MESSAGES,
                     token_budget: int = DEFAULT_TOKEN_BUDGET,
                     ) -> tuple[list[dict[str, Any]], int]:
    """把历史按预算装配成消息（确定性：不调模型、不猜语义；S7.10）。

    规则：最近的 `keep_messages` 条**保留原文**，更早的降级成摘要（正文前 60 字 + 一行工具
    结果摘要）；累计估算 token 超过 `token_budget` 就停下。最新一条永不丢——模型至少拿到
    最近一轮的上下文。返回（按时间正序的消息列表，被丢掉的消息条数）。

    **跨会话不串味由调用方保证**：只喂本会话的消息，本函数不做任何跨会话合并。
    """
    entries = [item for item in history
               if str(item.get("role") or "") in ("user", "assistant")]
    picked: list[tuple[str, str]] = []
    used = 0
    dropped = 0
    seen = 0
    for item in reversed(entries):
        text = _entry_text(item, digest=seen >= keep_messages)
        if not text:
            continue          # 正在生成中的 assistant 行（正文与工具都为空）不进上下文
        size = estimate_tokens(text)
        if picked and used + size > token_budget:
            dropped += 1
            continue
        picked.append((str(item.get("role")), text))
        used += size
        seen += 1
    return [{"role": role, "content": text} for role, text in reversed(picked)], dropped


def build_messages(rendered: prompt_tool.RenderedPrompt, history: list[dict[str, Any]],
                   user_text: str, *, attachment_note: str = "",
                   keep_messages: int = DEFAULT_KEEP_MESSAGES,
                   token_budget: int = DEFAULT_TOKEN_BUDGET) -> list[dict[str, Any]]:
    """组装给模型的消息序列：system 指令 → 任务说明 → 历史（预算内）→ 本轮用户消息。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": rendered.system},
        {"role": "user", "content": rendered.user},
    ]
    assembled, dropped = assemble_history(history, keep_messages=keep_messages,
                                         token_budget=token_budget)
    messages.extend(assembled)
    current = user_text.strip() or "（这条消息没有文字，只有附件）"
    notes: list[str] = []
    image_note = _earlier_image_note(history)
    if image_note:
        notes.append(image_note)
    if dropped:
        notes.append(f"本会话更早还有 {dropped} 条消息没装进上下文")
    if notes:
        current = f"{current}\n\n[历史] {'；'.join(notes)}"
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

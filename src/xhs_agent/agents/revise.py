"""文案改写：已有初稿 + 用户要求 → 改后初稿（`copy_revise`，S7.8）。

输入：线索 + 选中素材（可为 `None`）+ 已有初稿的 `to_dict()` + 用户要求 + `StructuredCaller`。
输出：`AgentOutcome(value=ReviseOutcome, version=prompt 版本)`——`ReviseOutcome` 要么是拒绝
      （`refused=True` + 一句理由），要么是一版通过 `Draft` 契约校验的新稿。
不做降级：prompt 缺变量、模型两次都解析失败、拒不拒绝又没给可用初稿，一律抛错。

边界复用：`copy_revise` 的 §03 逐字搬了 `copy_draft` §03 的 12 条边界，只多一条"越界必须
拒绝、不给变通做法"。改边界时两处一起改（prompt 契约 §六 的"一次只改一层"仍按任务各自成立）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..schemas import Draft, HotspotClue, SchemaError
from ..tools import prompt as prompt_tool
from ..tools.llm import StructuredCaller
from .base import AgentOutcome

TASK_ID = "copy_revise"


@dataclass(frozen=True)
class ReviseOutcome:
    """一次改写的结果：拒绝（带理由）或一版新稿，两者互斥。"""

    refused: bool
    reason: str = ""
    draft: Draft | None = None


def parse_revise(payload: Any, *, material_id: str = "",
                 hotspot_key: str = "") -> ReviseOutcome:
    """把模型输出解析成 `ReviseOutcome`（两种形状见 prompt 契约 §一 的对话工具清单）。"""
    if not isinstance(payload, dict):
        raise SchemaError(f"copy_revise 的输出必须是 JSON 对象，收到 {type(payload).__name__}")
    if bool(payload.get("refused")):
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise SchemaError("copy_revise 拒绝时必须在 reason 里写清理由")
        return ReviseOutcome(refused=True, reason=reason)
    raw = payload.get("draft")
    if not isinstance(raw, dict):
        raise SchemaError("copy_revise 没有拒绝时必须在 draft 里给出改后的初稿")
    draft = Draft.from_dict(raw, material_id=material_id, hotspot_key=hotspot_key)
    if not draft.body.strip():
        raise SchemaError("copy_revise 给回的初稿缺少正文（body）")
    return ReviseOutcome(refused=False, draft=draft)


async def revise_draft(clue: HotspotClue, material: dict[str, Any] | None,
                       draft: dict[str, Any], request: str,
                       caller: StructuredCaller,
                       constraints: list[dict[str, Any]] | None = None) -> AgentOutcome:
    """按用户要求改写一版已有初稿；越界时返回拒绝结果而不是硬改。

    `constraints` 是服务端筛选好的**适用事实快照**（A05：session + 当前话题 + 已验证产物）。
    它必须真正进入这次请求——不能只靠 supervisor 在对话里转述；prompt v2 起 `constraints_json`
    是必需变量。
    """
    rendered = prompt_tool.render(
        TASK_ID,
        draft_json=json.dumps(draft, ensure_ascii=False),
        user_request=request,
        clue_json=json.dumps(clue.to_dict(), ensure_ascii=False),
        material_json=(json.dumps(material, ensure_ascii=False) if material is not None
                       else "null"),
        constraints_json=json.dumps(list(constraints or []), ensure_ascii=False),
    )
    outcome, result = await caller.call(
        TASK_ID, rendered.system, rendered.user,
        parse=lambda payload: parse_revise(
            payload,
            material_id=str(draft.get("material_id") or ""),
            hotspot_key=str(draft.get("hotspot_key") or clue.hotspot_raw),
        ),
    )
    if outcome.draft is not None:
        # provider / model 由调用方注入，不从模型输出里读（与 clue.py / draft.py 同一口径）
        outcome = ReviseOutcome(
            refused=False, reason=outcome.reason,
            draft=outcome.draft.model_copy(update={"provider": result.provider,
                                                   "model": result.model}))
    return AgentOutcome(value=outcome, task_id=TASK_ID, version=rendered.version)

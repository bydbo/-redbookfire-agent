"""文案初稿：线索 + 选中素材 → 一版初稿（`copy_draft`）。

输入：线索 + 首选素材的 `to_dict()`（`None` 表示本次没有候选）+ `StructuredCaller` + 风格。
输出：`AgentOutcome(value=Draft, version=prompt 版本)`；没有素材时不调模型、`version=None`。
不做降级：prompt 缺变量、模型两次都解析失败、`Draft` 缺 `body`，一律抛错。
"""

from __future__ import annotations

import json
from typing import Any

from ..schemas import Draft, HotspotClue
from ..tools import prompt as prompt_tool
from ..tools.llm import StructuredCaller
from .base import AgentOutcome

TASK_ID = "copy_draft"
DEFAULT_STYLE = "真诚分享"


async def write_draft(clue: HotspotClue, material: dict[str, Any] | None,
                      caller: StructuredCaller, *,
                      style: str = DEFAULT_STYLE,
                      constraints: list[dict[str, Any]] | None = None) -> AgentOutcome:
    """给首选素材写一版文案初稿。

    `constraints` 是服务端筛选好的适用事实快照（A05）：worker 从本次运行的请求数据读取，
    与对话侧改稿使用**同一份**口径；prompt v2 起 `constraints_json` 是必需变量。
    """
    if material is None:
        return AgentOutcome(value=None, task_id=TASK_ID, version=None)

    rendered = prompt_tool.render(
        TASK_ID,
        hotspot_raw=clue.hotspot_raw,
        clue_json=json.dumps(clue.to_dict(), ensure_ascii=False),
        material_json=json.dumps(material, ensure_ascii=False),
        style=style or DEFAULT_STYLE,
        constraints_json=json.dumps(list(constraints or []), ensure_ascii=False),
    )
    draft, result = await caller.call(
        TASK_ID, rendered.system, rendered.user,
        parse=lambda payload: Draft.from_dict(
            payload, material_id=str(material.get("id") or ""),
            hotspot_key=clue.hotspot_key),
    )
    draft = draft.model_copy(update={"provider": result.provider, "model": result.model})
    return AgentOutcome(value=draft, task_id=TASK_ID, version=rendered.version)

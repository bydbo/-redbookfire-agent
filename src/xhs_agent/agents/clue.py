"""热点拆解：热点原文 → 爆点线索（`hotspot_clue`）。

输入：热点原文 + `StructuredCaller`。
输出：`AgentOutcome(value=HotspotClue, task_id="hotspot_clue", version=prompt 版本)`。
不做降级：prompt 缺变量、模型两次都解析失败、结构不合契约，一律抛错。
"""

from __future__ import annotations

from ..schemas import HotspotClue
from ..tools import prompt as prompt_tool
from ..tools.llm import StructuredCaller
from .base import AgentOutcome

TASK_ID = "hotspot_clue"


def extract_clue(hotspot_raw: str, caller: StructuredCaller) -> AgentOutcome:
    """调一次 `hotspot_clue`，返回通过契约校验的线索。"""
    rendered = prompt_tool.render(TASK_ID, hotspot_raw=hotspot_raw)
    clue, result = caller.call(
        TASK_ID, rendered.system, rendered.user,
        parse=lambda payload: HotspotClue.from_dict(payload, hotspot_raw=hotspot_raw),
    )
    # 契约（prompt 契约 §05）：hotspot_raw 必须与输入完全一致——from_dict 会优先取模型返回的
    # 那个字段，所以这里强制覆盖；provider / model 同样由调用方注入，不从模型输出里读。
    clue = clue.model_copy(update={"hotspot_raw": hotspot_raw.strip(),
                                   "provider": result.provider, "model": result.model})
    return AgentOutcome(value=clue, task_id=TASK_ID, version=rendered.version)

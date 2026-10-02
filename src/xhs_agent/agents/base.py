"""智能体共用的小结构。

`AgentOutcome` 把「领域对象」与「这次用的是哪个 prompt 的哪一版」放在一起——
`runs.prompt_versions` 就是靠它逐任务累积出来的（prompt 契约 §五）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentOutcome:
    """一次 agent 调用的结果。

    `version=None` 表示**本任务没有真正调用模型**（例如没有候选素材时不撰稿），
    此时不得写进 `prompt_versions`（prompt 契约 §五：未参与本次运行的任务不出现）。
    """

    value: Any
    task_id: str
    version: int | None

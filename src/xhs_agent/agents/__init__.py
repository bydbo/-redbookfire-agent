"""单点智能体：一个任务一个函数，薄封装在 prompt 渲染 + 结构化调用之上。

依赖方向：`agents/` 可用 `tools/` 与 `schemas`，但不碰状态机与数据库——
节点级编排在 `workflows/`，数据访问在 `services/`。
三个任务与 `docs/contracts/prompt契约.md` §一 的任务表一一对应；
每次调用都把 prompt 版本带出来（`AgentOutcome.version`），供 `runs.prompt_versions` 累积。
"""

from .base import AgentOutcome
from .clue import extract_clue
from .draft import DEFAULT_STYLE, write_draft
from .explain import explain_candidates

__all__ = [
    "DEFAULT_STYLE",
    "AgentOutcome",
    "explain_candidates",
    "extract_clue",
    "write_draft",
]

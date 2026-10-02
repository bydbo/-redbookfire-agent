"""流程编排：LangGraph 状态图与一次运行的入口。

依赖方向：`workflows/` 可以调 `agents/`、`services/` 与 `tools/`，但底层不得反向依赖它。
当前只有一条链路：`run_analysis` 逐热点跑「拆解 → 检索 → 缺口 → 撰稿 → 报告」。
"""

from .analysis import NODE_STEPS, RunResult, build_graph, run_analysis
from .state import AnalysisState

__all__ = [
    "NODE_STEPS",
    "AnalysisState",
    "RunResult",
    "build_graph",
    "run_analysis",
]

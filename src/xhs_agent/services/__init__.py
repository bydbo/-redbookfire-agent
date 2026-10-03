"""用例层：把 `tools/` / `agents/` / `db/` 的能力编排成一个个可复用的动作。

用途：放「一次场景化操作」——素材索引同步、热点分析、报告产出等；不实现底层算法，
      也不直接暴露 HTTP。依赖方向见 `AGENTS.md` 第 2 节：
      `workflows/ → services/ → {agents/, tools/, db/} → models/`。
输入：`AppConfig`、`AsyncSession` 与已注入的能力（如视觉打标可调用对象）。
输出：各服务自己的结果对象（如 `SyncReport`）。
"""

from .materials import (
    EmbeddingBackfillReport,
    SyncPlan,
    SyncReport,
    backfill_embeddings,
    plan_sync,
    row_to_material,
    sync_materials,
)
from .retrieval import RetrievalOutcome, VectorCoverage, retrieve_candidates
from .runs import Submission, load_job, load_report_model, load_run, submit_analysis

__all__ = [
    "EmbeddingBackfillReport",
    "RetrievalOutcome",
    "SyncPlan",
    "SyncReport",
    "Submission",
    "VectorCoverage",
    "backfill_embeddings",
    "load_job",
    "load_report_model",
    "load_run",
    "plan_sync",
    "retrieve_candidates",
    "row_to_material",
    "submit_analysis",
    "sync_materials",
]

"""阶段快照：对话轮次与分析运行的阶段口径（S7.19 冻结，S7.20 / S7.21 使用）。

用途：维护一份「当前阶段快照」字典——对话轮次的快照落 `chat_messages.progress`，
      分析运行的快照落 `runs.stage`；`label_for` / `error_category` 供 SSE 事件、结构化日志
      与追踪元数据共用同一份口径。
输入：前一版快照（可为 None）与阶段键 / 状态 / 尝试序号；输出：合并后的**新**快照字典
      （本模块不落库、不写日志，由调用方决定何时写）。
输出结构：`{stage, label, status, started_at, updated_at, attempt, retries, error_category,
      last_finished_stage, run_id, job_id, nodes}`，见《数据契约》§3.3 / §3.7。

口径（改动需同步 `docs/可观测.md` 与《数据契约》）：

- 只记阶段切换，不逐 token、不逐轮询；`nodes` 按 `(stage, attempt)` 去重，最多保留
  `MAX_NODES` 条（超出丢最早的）。
- **旧尝试的迟到更新不覆盖新状态**：同一阶段 `attempt` 更小的终态更新被忽略；同一阶段的
  重复 `running` 不重置 `started_at`。
- 错误只记**类别**（`ERROR_CATEGORIES` 白名单），不记错误正文、提示词、用户原文或素材路径。
- 按类名匹配异常（不在 `core/` 里 import `tools/*`，维持单向依赖）；未识别一律 `internal`。
"""

from __future__ import annotations

from typing import Any

from ..util import now_iso

#: `turn_id` 的 Celery 消息头：业务标识单独传播，不混进 W3C traceparent / tracestate
TURN_ID_HEADER = "x-xhs-turn-id"

#: 阶段键 → 中文标签（后端权威口径；前端 `stores/chat.ts` 有同一份展示映射）
STAGE_LABELS: dict[str, str] = {
    "supervisor": "理解与决策",
    "image_parse": "解析图片",
    "submit_analysis": "提交分析",
    "queue_wait": "排队等待",
    "material_sync": "素材同步",
    "embedding_backfill": "向量回填",
    "embedding_query": "向量化查询",
    "clue": "拆解",
    "retrieve": "检索",
    "gaps": "缺口",
    "draft": "撰稿",
    "report": "报告",
    "final_reply": "整理回复",
}

STAGE_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed", "skipped", "interrupted")
TERMINAL_STAGE_STATUSES: tuple[str, ...] = ("succeeded", "failed", "skipped", "interrupted")
MAX_NODES = 30

#: 工具内部的中文进度标签 → 阶段键（S7.20）；未登记的标签返回空串（前端退回显示原标签）。
#: 注意「分析中」是工具的临时进度文案（带 done/total），**不映射阶段键**——排队等待由
#: `runs.status = queued` 推导、worker 阶段由各自的标签（素材同步 / 拆解…）上报，
#: 否则轮询时会把当前阶段来回切回"排队等待"。
PROGRESS_LABEL_KEYS: dict[str, str] = {
    "解析图片": "image_parse",
    "提交分析": "submit_analysis",
    "拆解要素": "clue",
    "检索素材": "retrieve",
    "素材同步": "material_sync",
    "向量回填": "embedding_backfill",
}

ERROR_CATEGORIES: tuple[str, ...] = (
    "llm_error", "embedding_error", "schema_error", "config_error", "timeout",
    "cancelled", "dependency_unavailable", "internal",
)

_ERROR_BY_NAME: dict[str, str] = {
    "LLMError": "llm_error",
    "VisionError": "llm_error",
    "EmbeddingError": "embedding_error",
    "SchemaError": "schema_error",
    "ConfigError": "config_error",
    "PromptError": "config_error",
    "DependencyUnavailableError": "dependency_unavailable",
    "OperationalError": "dependency_unavailable",
    "InterfaceError": "dependency_unavailable",
    "ConnectionError": "dependency_unavailable",
    "ConnectError": "dependency_unavailable",
    "OSError": "dependency_unavailable",
    "TimeoutError": "timeout",
    "TimeoutException": "timeout",
    "ReadTimeout": "timeout",
    "PoolTimeout": "timeout",
    "SoftTimeLimitExceeded": "timeout",
    "CancelledError": "cancelled",
}


def label_for(stage: str) -> str:
    """阶段键 → 中文标签；未登记的键原样返回（便于新阶段灰度）。"""
    key = str(stage or "").strip()
    return STAGE_LABELS.get(key, key)


def key_for_label(label: str) -> str:
    """工具进度里的中文标签 → 阶段键；未登记返回空串（不臆造阶段）。"""
    value = str(label or "").strip()
    for key, text in STAGE_LABELS.items():
        if text == value:
            return key
    return PROGRESS_LABEL_KEYS.get(value, "")


def error_category(exc: BaseException) -> str:
    """异常 → 安全错误类别（白名单外一律 `internal`）；只返回类别，不含错误正文。"""
    for klass in type(exc).__mro__:
        category = _ERROR_BY_NAME.get(klass.__name__)
        if category:
            return category
    return "internal"


def _sanitize_error(category: str) -> str:
    value = str(category or "").strip()
    if not value:
        return ""
    return value if value in ERROR_CATEGORIES else "internal"


def empty() -> dict[str, Any]:
    """空快照（旧数据 / 未开始时的口径：`progress = {}` 读出来归一成这份结构）。"""
    return {
        "stage": "", "label": "", "status": "", "started_at": "", "updated_at": "",
        "attempt": 1, "retries": 0, "error_category": "", "last_finished_stage": "",
        "run_id": None, "job_id": None, "nodes": [],
    }


def normalize(prev: Any) -> dict[str, Any]:
    """把任意来源的快照（含 `{}` 与历史空值）归一到完整结构；不改传进来的对象。"""
    snapshot = empty()
    if isinstance(prev, dict):
        for key in snapshot:
            if key in prev:
                snapshot[key] = prev[key]
        nodes = prev.get("nodes")
        if isinstance(nodes, list):
            snapshot["nodes"] = [dict(item) for item in nodes
                                 if isinstance(item, dict)][-MAX_NODES:]
    snapshot["nodes"] = list(snapshot["nodes"])[-MAX_NODES:]
    snapshot["attempt"] = max(1, int(snapshot.get("attempt") or 1))
    snapshot["retries"] = max(0, int(snapshot.get("retries") or 0))
    return snapshot


def begin(prev: Any, *, stage: str, attempt: int = 1, retries: int = 0,
          run_id: str = "", job_id: str = "") -> dict[str, Any]:
    """进入一个阶段（`running`）；同一阶段重复进入只刷新 `updated_at`，不重置计时。"""
    snapshot = normalize(prev)
    attempt = max(1, int(attempt or 1))
    same = (snapshot.get("stage") == stage
            and int(snapshot.get("attempt") or 1) == attempt
            and snapshot.get("status") == "running")
    if same:
        snapshot["updated_at"] = now_iso()
        return snapshot
    snapshot.update({
        "stage": str(stage), "label": label_for(stage), "status": "running",
        "started_at": now_iso(), "updated_at": now_iso(),
        "attempt": attempt, "retries": max(0, int(retries or 0)),
        "error_category": "",
    })
    if run_id:
        snapshot["run_id"] = str(run_id)
    if job_id:
        snapshot["job_id"] = str(job_id)
    return snapshot


def end(prev: Any, *, stage: str, status: str, attempt: int = 1,
        error_category: str = "", latency_ms: int = 0) -> dict[str, Any]:
    """结束一个阶段并把它写进 `nodes`；迟到（`attempt` 更小）的更新被忽略。"""
    if status not in TERMINAL_STAGE_STATUSES:
        raise ValueError(f"阶段终态只能是 {TERMINAL_STAGE_STATUSES}，收到：{status!r}")
    snapshot = normalize(prev)
    attempt = max(1, int(attempt or 1))
    current_stage = str(snapshot.get("stage") or "")
    current_attempt = int(snapshot.get("attempt") or 1)
    if current_stage == stage and attempt < current_attempt:
        return snapshot                          # 旧尝试的迟到更新：不覆盖、不追加
    if current_stage in ("", stage):             # 结束的就是当前阶段（或还没有当前阶段）
        if current_stage == "":
            snapshot["stage"] = str(stage)
            snapshot["label"] = label_for(stage)
        snapshot["status"] = status
        snapshot["attempt"] = (max(attempt, current_attempt) if current_stage == stage
                               else attempt)
        snapshot["updated_at"] = now_iso()
        if error_category:
            snapshot["error_category"] = _sanitize_error(error_category)
    else:
        snapshot["updated_at"] = now_iso()       # 结束的不是当前阶段：只补 node
    node = {
        "stage": str(stage),
        "label": label_for(stage),
        "status": status,
        "attempt": attempt,
        "started_at": (snapshot.get("started_at") or ""
                       if current_stage == stage and attempt == current_attempt else ""),
        "ended_at": now_iso(),
        "latency_ms": max(0, int(latency_ms or 0)),
        "error_category": ("" if status in ("succeeded", "skipped")
                           else _sanitize_error(error_category)),
    }
    _upsert_node(snapshot, node)
    if status in ("succeeded", "skipped"):
        snapshot["last_finished_stage"] = str(stage)
    return snapshot


def apply(prev: Any, *, stage: str, status: str, attempt: int = 1, retries: int = 0,
          error_category: str = "", latency_ms: int = 0, run_id: str = "",
          job_id: str = "") -> dict[str, Any]:
    """`running` 走 `begin`、终态走 `end` 的统一入口（事件 / 阶段上报都走它）。"""
    if status == "running":
        return begin(prev, stage=stage, attempt=attempt, retries=retries,
                     run_id=run_id, job_id=job_id)
    return end(prev, stage=stage, status=status, attempt=attempt,
               error_category=error_category, latency_ms=latency_ms)


def interrupt(prev: Any, *, error_category: str = "timeout") -> dict[str, Any]:
    """把还在跑的当前阶段标成 `interrupted`（进程重启 / SSE 断开兜底用）。"""
    snapshot = normalize(prev)
    if snapshot.get("status") == "running":
        snapshot["status"] = "interrupted"
        snapshot["error_category"] = _sanitize_error(error_category)
        snapshot["updated_at"] = now_iso()
    return snapshot


def current_step(snapshot: Any) -> str | None:
    """`JobStatus.current_step` 的投影：仅在阶段确实在跑时给中文标签，否则为空。"""
    data = normalize(snapshot)
    if data.get("status") == "running" and data.get("label"):
        return str(data["label"])
    return None


def _upsert_node(snapshot: dict[str, Any], node: dict[str, Any]) -> None:
    """按 `(stage, attempt)` 去重写入节点记录；同一节点重复上报以最后一次为准。"""
    nodes = list(snapshot.get("nodes") or [])
    for index, item in enumerate(nodes):
        if (item.get("stage") == node["stage"]
                and int(item.get("attempt") or 1) == node["attempt"]):
            merged = dict(item)
            merged.update(node)
            merged["error_category"] = node["error_category"] or item.get("error_category", "")
            nodes[index] = merged
            break
    else:
        nodes.append(node)
    snapshot["nodes"] = nodes[-MAX_NODES:]

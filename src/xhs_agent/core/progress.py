"""阶段快照：对话轮次与分析运行的阶段口径（S7.19 冻结，S7.20 / S7.21 使用）。

用途：维护一份「当前阶段快照」字典——对话轮次的快照落 `chat_messages.progress`，
      分析运行的快照落 `runs.stage`；`label_for` / `error_category` 供 SSE 事件、结构化日志
      与追踪元数据共用同一份口径。
输入：前一版快照（可为 None）与阶段键 / 状态 / 尝试序号；输出：合并后的**新**快照字典
      （本模块不落库、不写日志，由调用方决定何时写）。
输出结构：`{stage, label, status, started_at, updated_at, attempt, retries, error_category,
      last_finished_stage, run_id, job_id, source_id, stage_instance_id, source_seq,
      progress_revision, nodes}`，见《数据契约》§3.3 / §3.7。

口径（改动需同步 `docs/可观测.md` 与《数据契约》）：

- 只记阶段切换，不逐 token、不逐轮询；`nodes` 按 `(stage, attempt, run_id?)` 去重，最多保留
  `MAX_NODES` 条（超出丢最早的）。`run_id` 是可选身份（R3）：对话轮次里同轮多次分析用它隔离，
  任一侧缺身份时按旧口径合并（兼容旧快照）。
- **单调合并（R3）**：同一阶段 `attempt` 更小的更新被忽略（开始与结束都查）；同一阶段重复
  `running` 不重置 `started_at`；同一执行的终态**不因重复 `begin` 重开**；只有更高的 `attempt`
  （重试）或新阶段才能启动新的运行。
- **身份隔离（R5 / E7OBS2-02）**：`run_id` / `job_id` 是**执行身份**。两侧都有明确身份且不一致
  时属于不同执行：
  - 迟到（旧执行）的终态**只补 `nodes` 历史事实，绝不改顶层当前状态**（不回退、不串身份）；
  - 顶层身份随当前阶段一致：切到新阶段时按事件身份重置，事件没带身份则清空
    （同一阶段内保留该阶段已知身份，兼容同阶段收尾不带身份的事件）。
- **冲突终态（R5）**：同一执行、同一 `attempt` 出现与首个终态**不同**的第二个终态时保留首个
  （不翻转、不混状态）；同状态重复上报允许刷新耗时（`latency_ms`）。
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
    # PR3 网页工具：tracing span 为 tool.web_search / tool.web_fetch
    "web_search": "搜索热点",
    "web_fetch": "读取网页",
}

STAGE_STATUSES: tuple[str, ...] = (
    "running",
    "succeeded",
    "failed",
    "skipped",
    "interrupted",
)
TERMINAL_STAGE_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "skipped",
    "interrupted",
)
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
    "搜索热点": "web_search",
    "读取网页": "web_fetch",
}

ERROR_CATEGORIES: tuple[str, ...] = (
    "llm_error",
    "embedding_error",
    "schema_error",
    "config_error",
    "timeout",
    "cancelled",
    "dependency_unavailable",
    "internal",
    # E7 第六批（S7.24–S7.27）：上下文可靠性的稳定失败类别（方案 §3.2）
    "required_context_overflow",
    "history_group_broken",
    "state_version_conflict",
    "artifact_version_mismatch",
    "cache_stale_or_invalid",
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
    # 上下文预算 / 结构 / 版本冲突：只记类别，不含正文（方案 §3.2）
    "BudgetError": "required_context_overflow",
    "HistoryGroupError": "history_group_broken",
    "StateVersionConflict": "state_version_conflict",
    "ArtifactVersionMismatch": "artifact_version_mismatch",
    "CacheInvalidError": "cache_stale_or_invalid",
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
        "stage": "",
        "label": "",
        "status": "",
        "started_at": "",
        "updated_at": "",
        "attempt": 1,
        "retries": 0,
        "error_category": "",
        "last_finished_stage": "",
        "run_id": None,
        "job_id": None,
        "source_id": "",
        "stage_instance_id": "",
        "source_seq": 0,
        "progress_revision": 0,
        "generation": 0,
        "nodes": [],
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
            snapshot["nodes"] = [
                dict(item) for item in nodes if isinstance(item, dict)
            ][-MAX_NODES:]
    snapshot["nodes"] = list(snapshot["nodes"])[-MAX_NODES:]
    snapshot["attempt"] = max(1, int(snapshot.get("attempt") or 1))
    snapshot["retries"] = max(0, int(snapshot.get("retries") or 0))
    snapshot["source_seq"] = max(0, int(snapshot.get("source_seq") or 0))
    snapshot["progress_revision"] = max(0, int(snapshot.get("progress_revision") or 0))
    snapshot["generation"] = max(0, int(snapshot.get("generation") or 0))
    return snapshot


def _identity_conflict(run_id: str, current_run: str) -> bool:
    """两侧都有明确执行身份且不一致：这是**另一条执行**的事件（R5 / E7OBS2-02）。

    迟到（旧执行）的终态只允许补 `nodes` 历史，不许改顶层当前状态；任一侧缺身份时
    按旧口径放行（兼容旧快照与不带身份的对话侧阶段）。
    """
    return bool(run_id) and bool(current_run) and run_id != current_run


def _next_identity(value: str, current: str, *, same_stage: bool) -> str | None:
    """推进顶层后的身份：事件带身份用它；没带时**同一阶段**保留该阶段已知身份，跨阶段清空。"""
    if value:
        return str(value)
    return (current or None) if same_stage else None


def begin(
    prev: Any,
    *,
    stage: str,
    attempt: int = 1,
    retries: int = 0,
    run_id: str = "",
    job_id: str = "",
    source_id: str = "",
    stage_instance_id: str = "",
    source_seq: int = 0,
    progress_revision: int = 0,
    generation: int = 0,
) -> dict[str, Any]:
    """进入一个阶段（`running`）；同一执行**实例**重复进入只刷新 `updated_at`。

    E7OBS4-03（先裁决后更新）：
    - 同一来源的旧序号事件（`source_seq < 当前序号`）：不接管、不重开，历史已由节点承载；
    - 旧执行代次（worker 重试前的那一代）的迟到写入：不接管；
    - 同一实例的更低 `attempt`：旧尝试的迟到开始，忽略；同一实例已终结：重复开始不重开；
    - **不同实例**（同名阶段的新热点 / 新一次执行）即使 `attempt` 相同也允许启动——
      节点查询与实例判断使用同一套身份键（source / 实例 / attempt），不再由旧三元组阻断。
    """
    snapshot = normalize(prev)
    attempt = max(1, int(attempt or 1))
    run_id = str(run_id or "")
    job_id = str(job_id or "")
    source_id = str(source_id or run_id or "")
    # 实例标识描述"哪一次执行"，不混入 attempt（重试属于同一实例）；生产端可显式提供更细的实例键
    stage_instance_id = str(stage_instance_id or f"{source_id}:{stage}")
    source_seq = max(0, int(source_seq or 0))
    generation = max(0, int(generation or 0))
    current_stage = str(snapshot.get("stage") or "")
    current_attempt = int(snapshot.get("attempt") or 1)
    current_run = str(snapshot.get("run_id") or "")
    current_source = str(snapshot.get("source_id") or "")
    current_instance = str(snapshot.get("stage_instance_id") or "")
    current_seq = int(snapshot.get("source_seq") or 0)
    current_generation = int(snapshot.get("generation") or 0)
    current_revision = int(snapshot.get("progress_revision") or 0)
    same_stage = current_stage == stage
    conflict = _identity_conflict(run_id, current_run)
    same_source = not (current_source and source_id and current_source != source_id)
    seq_known = bool(source_seq and current_seq and same_source)
    same_instance = not (
        current_instance and stage_instance_id and current_instance != stage_instance_id
    )
    if generation and current_generation and generation < current_generation:
        return snapshot  # 旧执行代次的迟到写入：不接管
    if seq_known and source_seq < current_seq:
        return snapshot  # 同来源旧事件：不接管、不重开
    if same_stage and not conflict:
        if not seq_known and same_instance and attempt < current_attempt:
            return snapshot  # 旧格式（无序号）按尝试号兜底
        if (
            attempt == current_attempt
            and same_instance
            and snapshot.get("status") in TERMINAL_STAGE_STATUSES
        ):
            return snapshot  # 同一实例已终结：重复开始不重开
    prior = _find_node(
        snapshot,
        stage=stage,
        attempt=attempt,
        run_id=run_id,
        source_id=source_id,
        stage_instance_id=stage_instance_id,
    )
    if prior is not None and str(prior.get("status") or "") in TERMINAL_STAGE_STATUSES:
        return snapshot  # 该实例已有终态：迟到开始不重开
    same = (
        same_stage
        and attempt == current_attempt
        and snapshot.get("status") == "running"
        and not conflict
        and same_instance
        and (not seq_known or source_seq >= current_seq)
    )
    if same:
        snapshot["updated_at"] = now_iso()
        if seq_known:
            snapshot["source_seq"] = source_seq
        return snapshot
    switched = bool(current_source and source_id and current_source != source_id)
    snapshot.update(
        {
            "stage": str(stage),
            "label": label_for(stage),
            "status": "running",
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "attempt": attempt,
            "retries": max(0, int(retries or 0)),
            "error_category": "",
            "source_id": source_id,
            "stage_instance_id": stage_instance_id,
            # 新来源从自己的序号起步；同来源只允许前进（旧事件已被上面的裁决拦下）
            "source_seq": source_seq if switched else max(source_seq, current_seq),
            "progress_revision": max(current_revision + 1, int(progress_revision or 0)),
            "generation": max(generation, current_generation),
        }
    )
    snapshot["run_id"] = _next_identity(run_id, current_run, same_stage=same_stage)
    snapshot["job_id"] = _next_identity(
        job_id, str(snapshot.get("job_id") or ""), same_stage=same_stage
    )
    return snapshot


def end(
    prev: Any,
    *,
    stage: str,
    status: str,
    attempt: int = 1,
    error_category: str = "",
    latency_ms: int = 0,
    retries: int = 0,
    run_id: str = "",
    job_id: str = "",
    source_id: str = "",
    stage_instance_id: str = "",
    source_seq: int = 0,
    progress_revision: int = 0,
    generation: int = 0,
) -> dict[str, Any]:
    """结束一个阶段并把它写进 `nodes`；旧来源 / 旧实例的迟到终态只补历史。

    E7OBS4-03：先裁决（来源 / 实例 / 序号 / 代次），再决定是否推进顶层：
    - 同来源旧序号、旧执行代次、身份冲突（旧 run）：只补 `nodes`，顶层一律不动；
    - 同一实例同一 `attempt`：同状态允许刷新耗时，**冲突终态保留首个**；
    - 同名阶段但**不同实例**：该终态属于另一实例，只补历史，不改写当前实例的状态；
    - 其余（可信的新事实）推进顶层，并把来源 / 实例 / 序号 / 代次一并更新——
      新来源从自己的序号起步，不取跨来源的 `max`。
    """
    if status not in TERMINAL_STAGE_STATUSES:
        raise ValueError(f"阶段终态只能是 {TERMINAL_STAGE_STATUSES}，收到：{status!r}")
    snapshot = normalize(prev)
    attempt = max(1, int(attempt or 1))
    run_id = str(run_id or "")
    job_id = str(job_id or "")
    source_id = str(source_id or run_id or "")
    # 实例标识描述"哪一次执行"，不混入 attempt（重试属于同一实例）；生产端可显式提供更细的实例键
    stage_instance_id = str(stage_instance_id or f"{source_id}:{stage}")
    source_seq = max(0, int(source_seq or 0))
    generation = max(0, int(generation or 0))
    current_stage = str(snapshot.get("stage") or "")
    current_attempt = int(snapshot.get("attempt") or 1)
    current_run = str(snapshot.get("run_id") or "")
    current_source = str(snapshot.get("source_id") or "")
    current_instance = str(snapshot.get("stage_instance_id") or "")
    current_seq = int(snapshot.get("source_seq") or 0)
    current_generation = int(snapshot.get("generation") or 0)
    current_revision = int(snapshot.get("progress_revision") or 0)
    conflict = _identity_conflict(run_id, current_run)
    same_stage = current_stage == stage
    same_source = not (current_source and source_id and current_source != source_id)
    seq_known = bool(source_seq and current_seq and same_source)
    same_instance = not (
        current_instance and stage_instance_id and current_instance != stage_instance_id
    )
    late = bool(
        (generation and current_generation and generation < current_generation)
        or (seq_known and source_seq < current_seq)
    )
    # 同一实例的既有历史节点（与 late / conflict 无关：冲突终态的裁决只看"这个实例结束过没有"）
    prior = _find_node(
        snapshot,
        stage=stage,
        attempt=attempt,
        run_id=run_id,
        source_id=source_id,
        stage_instance_id=stage_instance_id,
    )
    if (
        prior is not None
        and str(prior.get("status") or "") in TERMINAL_STAGE_STATUSES
        and str(prior.get("status") or "") != status
    ):
        # 冲突终态：保留首个结束事实（不翻转、不混状态）——迟到的另一种终态也不改写历史
        return snapshot
    if (
        not late
        and not conflict
        and same_stage
        and not seq_known
        and same_instance
        and attempt < current_attempt
    ):
        return snapshot  # 旧尝试的迟到更新：不覆盖、不追加
    if (
        not late
        and not conflict
        and not same_stage
        and prior is not None
        and str(prior.get("status") or "") in TERMINAL_STAGE_STATUSES
    ):
        late = True  # 该实例已有终态：重复终态只作历史
    if not late and not conflict and same_stage and not same_instance:
        late = True  # 同名阶段的另一实例：只补历史，不接管当前
    node: dict[str, Any] = {
        "stage": str(stage),
        "label": label_for(stage),
        "status": status,
        "attempt": attempt,
        "started_at": (
            str(snapshot.get("started_at") or "")
            if same_stage
            and attempt == current_attempt
            and not conflict
            and same_instance
            else ""
        ),
        "ended_at": now_iso(),
        "latency_ms": max(0, int(latency_ms or 0)),
        "error_category": (
            ""
            if status in ("succeeded", "skipped")
            else _sanitize_error(error_category)
        ),
        "source_id": source_id,
        "stage_instance_id": stage_instance_id,
        "source_seq": source_seq,
    }
    if generation:
        node["generation"] = generation
    if run_id:  # 身份可选：不传时保持旧快照形状（兼容）
        node["run_id"] = run_id
    _upsert_node(snapshot, node)
    if conflict or late:
        # 旧来源 / 旧实例 / 冲突身份的终态：历史节点已补，顶层当前状态不动
        return snapshot
    # 跨阶段推进且本阶段没有可续的起点：如实留空，不残留上一阶段的起点
    if not same_stage:
        prior = _find_node(
            snapshot,
            stage=stage,
            attempt=attempt,
            run_id=run_id,
            source_id=source_id,
            stage_instance_id=stage_instance_id,
        )
        snapshot["started_at"] = str(prior.get("started_at") or "") if prior else ""
    snapshot["label"] = label_for(stage)
    snapshot["stage"] = str(stage)
    snapshot["status"] = status
    snapshot["attempt"] = max(attempt, current_attempt) if same_stage else attempt
    snapshot["updated_at"] = now_iso()
    snapshot["source_id"] = source_id
    snapshot["stage_instance_id"] = stage_instance_id
    switched = bool(current_source and source_id and current_source != source_id)
    snapshot["source_seq"] = source_seq if switched else max(source_seq, current_seq)
    snapshot["progress_revision"] = max(
        current_revision + 1, int(progress_revision or 0)
    )
    snapshot["generation"] = max(generation, current_generation)
    # 无条件写：`retries` 表示"当前阶段的投递重试次数"，随阶段切换更新（R3）
    snapshot["retries"] = max(0, int(retries or 0))
    snapshot["run_id"] = _next_identity(run_id, current_run, same_stage=same_stage)
    snapshot["job_id"] = _next_identity(
        str(job_id or ""), str(snapshot.get("job_id") or ""), same_stage=same_stage
    )
    if same_stage:
        if error_category:
            snapshot["error_category"] = _sanitize_error(error_category)
    else:
        # 跨阶段推进：错误类别跟随本阶段（不残留自另一阶段）
        snapshot["error_category"] = (
            _sanitize_error(error_category) if error_category else ""
        )
    if status in ("succeeded", "skipped"):
        snapshot["last_finished_stage"] = str(stage)
    return snapshot


def apply(
    prev: Any,
    *,
    stage: str,
    status: str,
    attempt: int = 1,
    retries: int = 0,
    error_category: str = "",
    latency_ms: int = 0,
    run_id: str = "",
    job_id: str = "",
    source_id: str = "",
    stage_instance_id: str = "",
    source_seq: int = 0,
    progress_revision: int = 0,
    generation: int = 0,
) -> dict[str, Any]:
    """`running` 走 `begin`、终态走 `end` 的统一入口（事件 / 阶段上报都走它）。"""
    if status == "running":
        return begin(
            prev,
            stage=stage,
            attempt=attempt,
            retries=retries,
            run_id=run_id,
            job_id=job_id,
            source_id=source_id,
            stage_instance_id=stage_instance_id,
            source_seq=source_seq,
            progress_revision=progress_revision,
            generation=generation,
        )
    return end(
        prev,
        stage=stage,
        status=status,
        attempt=attempt,
        retries=retries,
        error_category=error_category,
        latency_ms=latency_ms,
        run_id=run_id,
        job_id=job_id,
        source_id=source_id,
        stage_instance_id=stage_instance_id,
        source_seq=source_seq,
        progress_revision=progress_revision,
        generation=generation,
    )


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


def _node_matches(
    item: dict[str, Any],
    *,
    stage: str,
    attempt: int,
    run_id: str,
    source_id: str = "",
    stage_instance_id: str = "",
) -> bool:
    """节点是否属于同一个执行：`(stage, attempt)` 相同，且身份不冲突。

    `run_id` 是 R3 新增的**可选**身份（对话轮次里同轮多次分析）：任一侧缺身份时按旧口径
    合并——旧快照（没有 `run_id` 的节点）因此保持可读、可继续更新。
    """
    if str(item.get("stage") or "") != stage:
        return False
    if int(item.get("attempt") or 1) != attempt:
        return False
    left, right = str(item.get("run_id") or ""), run_id
    if left and right and left != right:
        return False
    item_source = str(item.get("source_id") or "")
    if item_source and source_id and item_source != source_id:
        return False
    item_instance = str(item.get("stage_instance_id") or "")
    return not (
        item_instance and stage_instance_id and item_instance != stage_instance_id
    )


def _find_node(
    snapshot: dict[str, Any],
    *,
    stage: str,
    attempt: int,
    run_id: str = "",
    source_id: str = "",
    stage_instance_id: str = "",
) -> dict[str, Any] | None:
    """按 `(stage, attempt, run_id)` 找已有节点（R3 的单调合并与身份隔离都用它）。"""
    for item in snapshot.get("nodes") or []:
        if isinstance(item, dict) and _node_matches(
            item,
            stage=stage,
            attempt=attempt,
            run_id=run_id,
            source_id=source_id,
            stage_instance_id=stage_instance_id,
        ):
            return item
    return None


def _upsert_node(snapshot: dict[str, Any], node: dict[str, Any]) -> None:
    """按 `(stage, attempt, run_id)` 去重写入节点记录；同一节点重复上报以最后一次为准。"""
    nodes = list(snapshot.get("nodes") or [])
    for index, item in enumerate(nodes):
        if isinstance(item, dict) and _node_matches(
            item,
            stage=node["stage"],
            attempt=node["attempt"],
            run_id=str(node.get("run_id") or ""),
            source_id=str(node.get("source_id") or ""),
            stage_instance_id=str(node.get("stage_instance_id") or ""),
        ):
            merged = dict(item)
            merged.update(node)
            merged["error_category"] = node["error_category"] or item.get(
                "error_category", ""
            )
            nodes[index] = merged
            break
    else:
        nodes.append(node)
    snapshot["nodes"] = nodes[-MAX_NODES:]

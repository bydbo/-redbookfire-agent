"""API 层模型：把 `docs/contracts/openapi.yaml` 的 `components.schemas` 落到代码（S4.6）。

用途：三个 router 用它们声明 `response_model` / `responses`，让 FastAPI 导出的 OpenAPI 带上契约的
      形状；`scripts/check_openapi.py` 就是按这份对照做校验的（CI 的 test job 会跑它）。
输入：无（纯声明）；`AnalyzeRequest` 同时是 `POST /api/analyze` 的请求体。
输出：FastAPI 生成 `#/components/schemas/*`。

两条口径（改这里前先读）：

- **`extra="allow"`**：响应体允许比契约多字段——契约是**下界**不是上界（`RunDetail` 之类现在
  正好一一对应，但将来加字段不该让 pydantic 悄悄丢掉）。请求体反过来用 `extra="forbid"`，
  与契约的 `additionalProperties: false` 一致。
- **枚举用 `Literal`**：`str` 字段导出的是 `string`，只有 `Literal` 才会导出成契约那样的 `enum`；
  uuid / date-time 这类 `format` 用 `json_schema_extra` 声明（类型仍是 `str`，**不改变序列化**）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# ---- 枚举（取值集合必须与 openapi.yaml 一致；改这里要同步契约与单测）----
ElementType = Literal["ip", "topic", "scene", "visual", "emotion", "sound", "conflict",
                      "format", "audience"]
RunStatus = Literal["queued", "running", "succeeded", "failed"]
MaterialType = Literal["video", "image"]
MaterialSource = Literal["sidecar", "vision", "filename", "legacy"]
ComponentStatus = Literal["ok", "down"]
RecallSource = Literal["literal", "vector"]
ReportFormat = Literal["html", "md"]
ErrorCode = Literal["bad_request", "validation_error", "not_found", "conflict",
                    "payload_too_large",
                    "dependency_unavailable", "upstream_error", "internal_error"]

# 带 format 的字符串：类型仍是 str（序列化不变），只在 OpenAPI 里带上 format
UuidStr = Annotated[str, Field(json_schema_extra={"format": "uuid"})]
DateTimeStr = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
# 路径参数版：FastAPI 的 Path() 才能把 format 带进 parameters（类型仍是 str，非法 uuid 仍按 404 处理）
UuidPath = Annotated[str, Path(json_schema_extra={"format": "uuid"})]


class _Response(BaseModel):
    """响应模型基类：允许比契约多字段（契约是下界）。"""

    model_config = ConfigDict(extra="allow")


# ---- 阶段快照（S7.20 / S7.21）----
StageStatus = Literal["running", "succeeded", "failed", "skipped", "interrupted"]


class StageNode(_Response):
    """阶段快照里的一次阶段执行记录（契约 `StageNode`）。"""

    stage: str
    status: StageStatus
    label: str = ""
    attempt: int = Field(default=1, ge=1)
    started_at: DateTimeStr | None = None
    ended_at: DateTimeStr | None = None
    latency_ms: int = Field(default=0, ge=0)
    error_category: str = ""
    run_id: str | None = None
    source_id: str = ""
    stage_instance_id: str = ""
    source_seq: int = Field(default=0, ge=0)
    generation: int = Field(default=0, ge=0)


class StageInfo(_Response):
    """当前阶段快照（契约 `StageInfo`）；`current_step` 是它的 `label` 投影。"""

    stage: str
    status: StageStatus
    label: str = ""
    started_at: DateTimeStr | None = None
    updated_at: DateTimeStr | None = None
    attempt: int = Field(default=1, ge=1)
    retries: int = Field(default=0, ge=0)
    error_category: str = ""
    last_finished_stage: str = ""
    run_id: UuidStr | None = None
    job_id: str | None = None
    source_id: str = ""
    stage_instance_id: str = ""
    source_seq: int = Field(default=0, ge=0)
    progress_revision: int = Field(default=0, ge=0)
    generation: int = Field(default=0, ge=0)
    nodes: list[StageNode] = Field(default_factory=list)


class AnalyzeRequest(BaseModel):
    """`POST /api/analyze` 请求体（契约 `AnalyzeRequest`，未知字段一律拒绝）。

    `clues`（S6.7）与 `hotspots` 一一对应：非空项会被写进 `hotspots.clue` 并短路拆解节点；
    不传时行为与以前完全一致。
    """

    model_config = ConfigDict(extra="forbid")

    hotspots: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                    max_length=500)]] = Field(
        min_length=1, max_length=10)
    topk: int = Field(default=5, ge=1, le=20)
    clues: list[HotspotClue | None] | None = Field(default=None, min_length=1, max_length=10)


class UpdateMaterialRequest(BaseModel):
    """`PATCH /api/materials/{material_id}` 请求体（S6.2）：白名单三字段，其余一律拒绝。

    "至少要给一个字段"（否则 400）与标签的去重 / 上限（超限 400）都在路由层判断：
    它们不是字段级类型错误，契约把 422 留给"多字段 / 类型不对"这一档。
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    tags: list[str] | None = None
    description: str | None = None


class AnalyzeAccepted(_Response):
    """202 受理结果：轮询用 `job_id`，取结果与报告用 `run_id`。"""

    job_id: str
    run_id: UuidStr


class ErrorResponse(_Response):
    """统一错误响应（与 `api/errors.py` 的处理器输出一致）。"""

    code: ErrorCode
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class JobStatus(_Response):
    """`GET /api/jobs/{job_id}`：进度由"已完成热点数 / 总数"推导。"""

    job_id: str
    run_id: UuidStr
    status: RunStatus
    progress: float
    current_step: str | None = None
    stage: StageInfo | None = None
    error: ErrorResponse | None = None


class RunTotals(_Response):
    """一次运行的 LLM 汇总（与 `runs` 表同名统计列一致）。"""

    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_cny: float
    latency_ms: int


class Mechanism(_Response):
    name: str
    explain: str = ""


class Element(_Response):
    """一个爆点要素（九类之一）。"""

    type: ElementType
    value: str
    weight: float = 0.6
    confidence: float = 0.7
    evidence: str = ""


class HotspotClue(_Response):
    why_it_works: list[str]
    mechanisms: list[Mechanism]
    elements: list[Element]
    match_keywords: list[str] = Field(default_factory=list)
    audience: dict[str, Any] = Field(default_factory=dict)
    borrow_angles: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class Coverage(_Response):
    ratio: float
    covered: list[Element]
    gaps: list[Element]


class MatchHit(_Response):
    element_type: ElementType
    clue_value: str
    hit_value: str = ""
    similarity: float | None = None
    contribution: float | None = None


class MaterialSummary(_Response):
    """候选素材摘要（报告里的关键帧缩略图也读这里）。"""

    id: UuidStr
    path: str
    type: MaterialType
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    keyframes: list[str] = Field(default_factory=list)


class MaterialItem(_Response):
    """素材库列表项（S6.1）；契约里这些字段全部必填，所以这里不给默认值。"""

    id: UuidStr
    path: str
    type: MaterialType
    title: str
    description: str
    tags: list[str]
    duration_s: float
    width: int
    height: int
    has_audio: bool
    size_bytes: int
    source: MaterialSource
    keyframes: list[str]
    indexed_at: DateTimeStr
    dir: str


class MaterialDetail(_Response):
    """素材详情（S6.1）：列表项的全部字段 + 爆点要素。

    刻意**不用继承**：pydantic 继承会让 FastAPI 导出 `allOf`，而 `scripts/check_openapi.py`
    的递归形状比对不认识 `allOf`，契约里也就跟着扁平写全（两处字段必须始终一致）。
    """

    id: UuidStr
    path: str
    type: MaterialType
    title: str
    description: str
    tags: list[str]
    duration_s: float
    width: int
    height: int
    has_audio: bool
    size_bytes: int
    source: MaterialSource
    keyframes: list[str]
    indexed_at: DateTimeStr
    dir: str
    elements: list[Element]


class MaterialDirCount(_Response):
    """主题目录及其素材数（`MaterialList.dirs` 的元素）。"""

    path: str
    count: int


class MaterialList(_Response):
    """`GET /api/materials` 响应：分页列表 + 主题目录聚合。"""

    items: list[MaterialItem]
    total: int
    limit: int
    offset: int
    dirs: list[MaterialDirCount]


class MaterialTrashResult(_Response):
    """`POST /api/materials/{material_id}/trash` 的结果（S6.3）。"""

    material_id: UuidStr
    path: str                 # 移入前的相对路径（原样，不带序号）
    trashed_path: str         # 回收站里的相对路径（同名时带 `-2` 序号）
    file_missing: bool        # 磁盘上本来就没有这个文件：只删了索引行


class TrashItem(_Response):
    """回收站里的一条素材（旁车跟着媒体走，不单独列出）。"""

    path: str                 # 相对 `_trash/` 的路径，正斜杠
    name: str
    type: MaterialType
    size_bytes: int
    mtime: DateTimeStr


class TrashList(_Response):
    """`GET /api/materials/trash` 响应：回收站内容。"""

    items: list[TrashItem]
    total: int


class TrashRestoreRequest(BaseModel):
    """恢复请求（S6.3）：`path` 是回收站里的相对路径（正斜杠）。"""

    model_config = ConfigDict(extra="forbid")

    path: str


class TrashPurgeRequest(BaseModel):
    """真删请求（S6.3）：`paths` 与 `all=true` 二选一，且 `confirm` 必须为 true。

    `confirm` 的"必须为 true"在路由层判断（400）——契约里只声明它是个布尔，
    免得把"二次确认"这种业务规则塞进字段级校验里。
    """

    model_config = ConfigDict(extra="forbid")

    paths: list[str] | None = None
    all: bool = False
    confirm: bool = False


class TrashPurgeResult(_Response):
    """真删结果：被删掉的相对路径（含跟随媒体一起删的旁车）。"""

    deleted: list[str]
    count: int


class MaterialTaskAccepted(_Response):
    """素材索引任务已受理（S6.3 起）：`task_id` 用于 `GET /api/materials/tasks/{task_id}`。"""

    task_id: str


class MaterialUploadAccepted(_Response):
    """上传受理结果（S6.4）：文件已落盘，索引任务已投递。"""

    task_id: str
    path: str        # 相对素材根目录的正斜杠路径（`<YYYY-MM>/名称.mp4`）
    name: str        # 实际落盘的文件名（同名时带 `-2` 序号）


class MaterialTask(_Response):
    """素材索引任务的状态（S6.4）：阶段、结果计数与失败原因。

    `state` 取 Celery 的标准五态；结果键有生命周期（1 小时），过期后回到 `PENDING`。
    """

    task_id: str
    state: Literal["PENDING", "STARTED", "RETRY", "SUCCESS", "FAILURE"]
    step: str | None = None
    summary: str | None = None
    result: dict[str, Any] | None = None


class ChatSessionCreate(BaseModel):
    """`POST /api/chat/sessions` 请求体（可省）：标题留空即等首条用户消息自动补。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=100)


class ChatAttachment(_Response):
    """对话消息里的附件引用（S7.2/S7.4）：`url` 可直接当 `<img src>`。"""

    # pydantic 对**单值** Literal 只导出 `const`，而契约与自写比对器认的是 `enum`：
    # 补一份 enum 让两边可比（取值范围仍是同一个 "image"）。
    kind: Annotated[Literal["image"], Field(json_schema_extra={"enum": ["image"]})]
    name: str
    mime: str
    bytes: int
    url: str


class ChatToolCall(_Response):
    """一次工具调用的过程记录（挂在 assistant 消息上）。"""

    tool: str
    status: Literal["succeeded", "failed", "interrupted"]
    args: dict[str, Any]
    call_id: str = ""
    attempt: int = Field(default=1, ge=1)
    run_id: UuidStr | None = None
    cost_cny: float = 0.0
    # 这次工具调用用掉的 token（含图片解析与分析那次 run 的用量，S7.10）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    error: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    # E7 第六批（S7.25）：改稿的实际基础版本与成功版本（前端据此显示版本链）
    object_key: str = ""
    version_id: UuidStr | None = None
    version_no: int | None = None
    base_version_no: int | None = None


class ChatMessage(_Response):
    """一条对话消息（`role` 只会有 user / assistant，工具过程挂在 `tool_calls`）。"""

    message_id: UuidStr
    session_id: UuidStr
    role: Literal["user", "assistant"]
    content: str
    status: Literal["running", "succeeded", "failed", "interrupted"]
    attachments: list[ChatAttachment]
    tool_calls: list[ChatToolCall]
    prompt_versions: dict[str, int]
    created_at: DateTimeStr
    run_id: UuidStr | None = None
    cost_cny: float = 0.0
    # 本轮用量 = supervisor 调用 + 各工具内部模型调用（含图片解析）之和，S7.10 起落库
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    error: str | None = None
    # S7.21：本轮标识、trace 关联与可刷新的阶段快照（旧行为 None / None / 空快照）
    turn_id: UuidStr | None = None
    trace_id: str | None = None
    trace_url: str | None = None
    progress: StageInfo | None = None


class ChatSession(_Response):
    """一个对话会话（S7.2）。"""

    session_id: UuidStr
    title: str
    created_at: DateTimeStr
    updated_at: DateTimeStr


class ChatSessionSummary(_Response):
    """会话列表项：会话字段 + 消息数与末条预览（刻意不用继承，避免导出 `allOf`）。"""

    session_id: UuidStr
    title: str
    created_at: DateTimeStr
    updated_at: DateTimeStr
    message_count: int
    last_message_preview: str


class ChatSessionList(_Response):
    """`GET /api/chat/sessions` 响应。"""

    items: list[ChatSessionSummary]
    total: int


# ---- E7 第六批（S7.24 / S7.25）：上下文状态与产物版本 ----
FactKind = Literal["goal", "constraint", "preference", "audience", "format"]
FactStatus = Literal["active", "pending_confirmation", "superseded", "retracted"]
FactScope = Literal["session", "topic", "artifact"]
ContextAction = Literal["confirm", "reject", "retract", "replace", "reset_topic",
                        "select_object", "recover", "complete_recovery"]
ArtifactStatus = Literal["pending", "succeeded", "failed", "refused", "interrupted"]
ArtifactType = Literal["draft", "clue"]
ContextMode = Literal["off", "shadow", "enforce"]


class ContextFact(_Response):
    """一条会话事实（契约 `ContextFact`）。"""

    fact_id: str
    kind: FactKind
    value: str
    scope: FactScope
    status: FactStatus
    evidence: str
    created_version: int = Field(ge=0)
    scope_id: str = ""
    source_ref: dict[str, Any] = Field(default_factory=dict)
    replaces: str | None = None
    replaces_fact_ids: list[str] = Field(default_factory=list)
    updated_at: DateTimeStr = ""


class ContextProposal(_Response):
    """一条待确认提案（契约 `ContextProposal`）。"""

    proposal_id: str
    kind: FactKind
    value: str
    scope: FactScope
    reason: str
    scope_id: str = ""
    evidence: str = ""
    source_ref: dict[str, Any] = Field(default_factory=dict)
    conflicts_with: list[str] = Field(default_factory=list)
    created_version: int = Field(default=0, ge=0)
    operation: Literal["add", "replace", "retract", "reset_topic", "select_object"] = "add"
    replaces_fact_ids: list[str] = Field(default_factory=list)
    target_object_ref: dict[str, Any] = Field(default_factory=dict)
    source_message_ids: list[str] = Field(default_factory=list)


class ObjectRef(_Response):
    """一个已验证的对象引用（v2 §3.3A；空字段表示尚未选择，不得以空值激活产物级约束）。"""

    object_key: str
    object_type: ArtifactType
    topic_id: str = ""
    run_id: UuidStr | None = None
    hotspot_position: int | None = None
    material_id: str | None = None
    version_id: UuidStr | None = None
    image_group_digest: str | None = None
    source_ref: dict[str, Any] = Field(default_factory=dict)


class RecoveryProgress(_Response):
    """旧会话恢复进度（v2 §3.3A / §3.5）。"""

    status: Literal["complete", "pending", "running", "awaiting_confirmation",
                    "interrupted", "failed"]
    recovery_id: UuidStr | None = None
    reason: str = ""
    # A01：维护起点（新会话创建时写入；旧会话首次恢复时取历史末条）
    baseline_created_at: DateTimeStr | None = None
    baseline_message_id: UuidStr | None = None
    snapshot_last_message_id: UuidStr | None = None
    cursor_message_id: UuidStr | None = None
    scanned_user_messages: int = Field(default=0, ge=0)
    total_user_messages: int = Field(default=0, ge=0)
    pending_proposal_ids: list[str] = Field(default_factory=list)
    accounting_message_id: UuidStr | None = None
    page_action_id: UuidStr | None = None
    gaps: list[dict[str, Any]] = Field(default_factory=list)


class ArtifactHead(_Response):
    """产物对象当前指向的成功版本（契约 `ArtifactHead`）。"""

    object_key: str
    version_id: UuidStr
    version_no: int = Field(ge=1)
    updated_at: DateTimeStr = ""


class ChatContext(_Response):
    """会话上下文（契约 `ChatContext`）。"""

    session_id: UuidStr
    state_version: int = Field(ge=0)
    recovery_status: Literal["complete", "partial"]
    facts: list[ContextFact]
    proposals: list[ContextProposal]
    artifact_heads: list[ArtifactHead]
    effective_mode: ContextMode
    topic_id: str | None = None
    blocking_proposals: int = Field(default=0, ge=0)
    effective_mode_reason: str = ""
    object_refs: list[ObjectRef] = Field(default_factory=list)
    recovery: RecoveryProgress | None = None


class ContextActionRequest(BaseModel):
    """`POST .../context/actions` 请求体（契约 `ContextActionRequest`，v2 §3.3A）。"""

    model_config = ConfigDict(extra="forbid")

    action: ContextAction
    expected_state_version: int = Field(ge=0)
    action_id: UuidStr | None = None
    target_id: str | None = None
    value: str | None = None
    object_ref: dict[str, Any] | None = None
    recovery_id: UuidStr | None = None
    retry_of: UuidStr | None = None
    acknowledge_partial_history: bool = False


class ContextActionResponse(_Response):
    """动作结果与最新上下文（契约 `ContextActionResponse`，v2 §3.3A）。"""

    session_id: UuidStr
    state_version: int = Field(ge=0)
    action: Literal["confirm", "reject", "retract", "replace", "reset_topic",
                    "select_object", "recover", "complete_recovery", "edit_clue", "select_clue"]
    decision: Literal["applied", "noop", "conflict"]
    context: ChatContext
    reason: str = ""
    action_id: UuidStr = ""
    accounting_message_id: UuidStr | None = None
    replay: bool = False
    status: Literal["pending", "running", "succeeded", "failed", "interrupted"] = "succeeded"
    version_id: UuidStr | None = None
    version: ArtifactVersion | None = None


class ArtifactVersion(_Response):
    """一个不可变的文案 / 线索版本（契约 `ArtifactVersion`）。"""

    version_id: UuidStr
    session_id: UuidStr
    object_key: str
    object_type: ArtifactType
    status: ArtifactStatus
    prompt_versions: dict[str, int]
    usage: dict[str, Any]
    created_at: DateTimeStr
    run_id: UuidStr | None = None
    hotspot_position: int | None = None
    version_no: int | None = None
    base_version_id: UuidStr | None = None
    content: dict[str, Any] | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    evidence_snapshot: dict[str, Any]
    error: str | None = None
    updated_at: DateTimeStr = ""


class ArtifactVersionList(_Response):
    """版本列表（契约 `ArtifactVersionList`）。"""

    items: list[ArtifactVersion]
    total: int = Field(ge=0)


class ClueVersionRequest(BaseModel):
    """`POST .../artifacts/clue-versions` 请求体（v2 §3.3C）。"""

    model_config = ConfigDict(extra="forbid")

    action_id: UuidStr
    expected_state_version: int = Field(ge=0)
    expected_head_version_id: UuidStr | None = None
    object_key: str
    base_version_id: UuidStr
    content: dict[str, Any]


class ClueSelectRequest(BaseModel):
    """`POST .../artifacts/{version_id}/select` 请求体（v2 §3.3C）。"""

    model_config = ConfigDict(extra="forbid")

    action_id: UuidStr
    expected_state_version: int = Field(ge=0)


class ChatSessionDetail(_Response):
    """会话详情：会话字段 + 消息数组（同样扁平写全）。

    E7 第六批（S7.24）：可选 `context`——上下文状态摘要（旧客户端忽略即可）。
    """

    session_id: UuidStr
    title: str
    created_at: DateTimeStr
    updated_at: DateTimeStr
    messages: list[ChatMessage]
    context: ChatContext | None = None


EvidenceOrigin = Literal["runtime", "legacy_run_record", "migration_capture",
                         "cutover_capture", "missing"]
EvidenceCompleteness = Literal["complete", "partial", "missing"]


class EvidenceMeta(_Response):
    """字段组来源 / 完整度 / 摘要；origin 与 completeness 独立。"""

    schema_version: int
    origin: EvidenceOrigin
    captured_at: DateTimeStr
    source_ref: str
    digest: str
    completeness: EvidenceCompleteness
    missing_fields: list[str]


class HistoricalKeyframeContent(_Response):
    """捕获时可验证的图片内容；路径清单摘要不能代替此证据。"""

    path: str
    sha256: str
    size_bytes: int


class HistoricalMaterial(_Response):
    """候选在运行当时实际使用的素材摘要（不读当前库覆盖）。"""

    id: UuidStr
    path: str
    type: MaterialType
    title: str
    description: str
    tags: list[str]
    elements: list[Element]
    duration_s: float
    width: int
    height: int
    has_audio: bool
    size_bytes: int
    mtime: DateTimeStr | None
    source: MaterialSource
    keyframes: list[str]
    indexed_at: DateTimeStr | None
    fingerprint: str
    embedding_model: str | None
    original_material_id: UuidStr | None = None
    keyframe_contents: list[HistoricalKeyframeContent] = Field(default_factory=list)


class CurrentMaterialStatus(_Response):
    """当前素材是否还在、文件是否可读、预览是否可用；不写回历史字段。"""

    exists: bool
    file_available: bool | None
    preview_available: bool
    reason: str | None
    path: str | None
    title: str | None


class MatchCandidate(_Response):
    rank: int
    material_id: UuidStr
    score: float
    recall_sources: list[RecallSource]
    hits: list[MatchHit]
    missing: list[Element]
    reasons: list[str] = Field(min_length=1)
    usage: str
    material: HistoricalMaterial | None
    current_material: CurrentMaterialStatus
    evidence: dict[str, EvidenceMeta]


class DraftTitle(_Response):
    text: str
    style: str = ""


class Draft(_Response):
    titles: list[DraftTitle]
    body: str
    tags: list[str]
    cover_text: str = ""
    first_3s: str = ""
    shot_list: list[str] = Field(default_factory=list)
    compliance_notes: list[str] = Field(default_factory=list)


class HotspotResult(_Response):
    position: int = Field(ge=1)
    hotspot_id: UuidStr
    hotspot_raw: str
    clue: HotspotClue | None
    status: RunStatus
    error: str | None
    coverage: Coverage | None
    retrieval_meta: dict[str, Any]
    gap_advice: dict[str, Any] | None
    candidates: list[MatchCandidate]
    draft: Draft | None
    evidence: dict[str, EvidenceMeta]


class RunDetail(_Response):
    run_id: UuidStr
    status: RunStatus
    created_at: DateTimeStr
    finished_at: DateTimeStr | None = None
    totals: RunTotals
    prompt_versions: dict[str, int]
    execution_snapshot: dict[str, Any]
    evidence: dict[str, EvidenceMeta]
    hotspots: list[HotspotResult]


class RunSummary(_Response):
    """`GET /api/runs` 的一条历史运行摘要（S5.6）。

    必填项严格对齐契约：pydantic 里给默认值会让导出的 `required` 少一项，
    `scripts/check_openapi.py` 会直接报差异；因此只有契约声明可空的
    `finished_at` / `error` 才带默认值。
    """

    run_id: UuidStr
    status: RunStatus
    created_at: DateTimeStr
    hotspot_count: int
    hotspot_preview: str
    topk: int
    totals: RunTotals
    finished_at: DateTimeStr | None = None
    error: str | None = None


class RunList(_Response):
    """`GET /api/runs` 响应：分页摘要；`total` 是全部条数（供分页器用），不是本页条数。"""

    items: list[RunSummary]
    total: int


class ComponentHealth(_Response):
    status: ComponentStatus
    detail: str = ""
    latency_ms: int | None = None


class HealthChecks(_Response):
    db: ComponentHealth
    redis: ComponentHealth
    llm: ComponentHealth


class Health(_Response):
    status: ComponentStatus
    checks: HealthChecks


class ImageClueResult(_Response):
    """图片拆解结果（S6.6）：**不落库**，用户确认后随 `AnalyzeRequest.clues` 提交。"""

    raw_text: str
    clue: HotspotClue
    prompt_versions: dict[str, int]


def error_response(description: str) -> dict[str, Any]:
    """构造 `responses={...}` 里的错误响应声明（body 统一是 `ErrorResponse`）。"""
    return {"model": ErrorResponse, "description": description}

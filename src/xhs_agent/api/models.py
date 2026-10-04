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
ComponentStatus = Literal["ok", "down"]
RecallSource = Literal["literal", "vector"]
ReportFormat = Literal["html", "md"]
ErrorCode = Literal["bad_request", "validation_error", "not_found", "conflict",
                    "dependency_unavailable", "upstream_error", "internal_error"]

# 带 format 的字符串：类型仍是 str（序列化不变），只在 OpenAPI 里带上 format
UuidStr = Annotated[str, Field(json_schema_extra={"format": "uuid"})]
DateTimeStr = Annotated[str, Field(json_schema_extra={"format": "date-time"})]
# 路径参数版：FastAPI 的 Path() 才能把 format 带进 parameters（类型仍是 str，非法 uuid 仍按 404 处理）
UuidPath = Annotated[str, Path(json_schema_extra={"format": "uuid"})]


class _Response(BaseModel):
    """响应模型基类：允许比契约多字段（契约是下界）。"""

    model_config = ConfigDict(extra="allow")


class AnalyzeRequest(BaseModel):
    """`POST /api/analyze` 请求体（契约 `AnalyzeRequest`，未知字段一律拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    hotspots: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                    max_length=500)]] = Field(
        min_length=1, max_length=10)
    topk: int = Field(default=5, ge=1, le=20)


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


class MatchCandidate(_Response):
    rank: int
    material_id: UuidStr
    score: float
    recall_sources: list[RecallSource]
    hits: list[MatchHit]
    missing: list[Element]
    reasons: list[str] = Field(min_length=1)
    usage: str
    material: MaterialSummary


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
    hotspot_id: UuidStr
    hotspot_raw: str
    clue: HotspotClue
    coverage: Coverage
    candidates: list[MatchCandidate]
    draft: Draft | None = None


class RunDetail(_Response):
    run_id: UuidStr
    status: RunStatus
    created_at: DateTimeStr
    finished_at: DateTimeStr | None = None
    totals: RunTotals
    prompt_versions: dict[str, int]
    hotspots: list[HotspotResult]


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


def error_response(description: str) -> dict[str, Any]:
    """构造 `responses={...}` 里的错误响应声明（body 统一是 `ErrorResponse`）。"""
    return {"model": ErrorResponse, "description": description}

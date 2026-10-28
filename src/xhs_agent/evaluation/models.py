"""E9 评测记录模型：数据、尝试、费用、人工评分与判定。

数据格式是 E9 协议的可执行事实源；本模块只描述结构，不做文件 I/O 与业务计算。
所有记录都禁止未知字段，避免标注或账本字段拼写错误被静默忽略。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Split = Literal["dev", "holdout"]
InputType = Literal["text", "single_image", "multi_same_topic", "multi_mixed"]
ScenarioTag = Literal["natural_naming", "synonym", "vague", "wrong_label", "cross_scene"]
Strategy = Literal["keyword", "trgm_only", "vector_only", "hybrid"]
ClueMode = Literal["oracle", "real"]
AttemptStatus = Literal["completed", "failed", "timeout", "not_run"]
RequestKind = Literal["text", "vision", "embedding"]
RequestStatus = Literal["reserved", "settled", "unknown"]
Verdict = Literal["pass", "fail", "uncertain"]
GateName = Literal["evidence", "quality", "benefit"]
GateStatus = Literal["pass", "fail", "incomplete"]

STRATEGIES: tuple[Strategy, ...] = ("keyword", "trgm_only", "vector_only", "hybrid")
SPLITS: tuple[Split, ...] = ("dev", "holdout")
INPUT_TYPES: tuple[InputType, ...] = (
    "text",
    "single_image",
    "multi_same_topic",
    "multi_mixed",
)
SCENARIO_TAGS: tuple[ScenarioTag, ...] = (
    "natural_naming",
    "synonym",
    "vague",
    "wrong_label",
    "cross_scene",
)


class EvalModel(BaseModel):
    """E9 记录基类：未知字段直接报错，冻结后不可原地改。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ImageRef(EvalModel):
    """有序图片引用；hash 必须与素材清单中的字节摘要一致。"""

    material_key: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class NaturalInput(EvalModel):
    """自然输入：原文 + 有序图片；图片顺序就是用户上传顺序。"""

    text: str = ""
    images: list[ImageRef] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _text_strip(cls, value: str) -> str:
        return value.strip()


class OracleElement(EvalModel):
    """人工确认的 oracle 线索要素；不得由模型结果补写。"""

    type: str = Field(min_length=1)
    value: str = Field(min_length=1)

    @field_validator("type", "value")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class Oracle(EvalModel):
    """真实拆解之外的 oracle 线索，只用于检索层对照。"""

    elements: list[OracleElement] = Field(default_factory=list, min_length=1)
    match_keywords: list[str] = Field(default_factory=list)

    @field_validator("match_keywords")
    @classmethod
    def _keywords(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            text = value.strip()
            if text and text not in out:
                out.append(text)
        return out


class TaskExpectation(EvalModel):
    """任务预期：语义验收由人工确认，自动检查只覆盖结构与引用。"""

    answerable: bool
    summary: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class Case(EvalModel):
    """一个自然任务；分区和分组不得跨集。"""

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    group_id: str = Field(min_length=1)
    split: Split
    input_type: InputType
    natural_input: NaturalInput
    expectation: TaskExpectation
    oracle: Oracle
    scenario_tags: list[ScenarioTag] = Field(default_factory=list)
    e2e: bool = False
    notes: str = ""


class MaterialAsset(EvalModel):
    """素材清单条目；真实文件可留在忽略目录，但摘要和授权必须入库。"""

    material_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image", "video", "text"]
    source: str = Field(min_length=1)
    license: str = Field(min_length=1)
    model_transfer_allowed: bool
    consent_ref: str = ""

    @field_validator("relative_path")
    @classmethod
    def _relative_only(cls, value: str) -> str:
        text = value.replace("\\", "/").strip()
        if text.startswith("/") or (len(text) > 2 and text[1] == ":"):
            raise ValueError("素材路径必须是相对路径")
        if ".." in text.split("/"):
            raise ValueError("素材路径不得包含 ..")
        return text


class Qrel(EvalModel):
    """任务—素材相关性；稀疏保存正例时仍要有全库审阅记录。"""

    case_id: str = Field(min_length=1)
    material_key: str = Field(min_length=1)
    grade: Literal[0, 1, 2]
    reason: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    confirmed: bool


class CaseReview(EvalModel):
    """一条任务已完成全库审阅的证据；数量必须等于冻结时素材库大小。"""

    case_id: str = Field(min_length=1)
    reviewed_materials: int = Field(ge=0)
    library_size: int = Field(ge=0)
    reviewer: str = Field(min_length=1)
    confirmed_at: str = Field(min_length=1)


class E2ETask(EvalModel):
    """S9.3 固定任务：repeats 表示额外重复次数（0 或 2）。"""

    case_id: str = Field(min_length=1)
    repeats: Literal[0, 2] = 0
    has_followup: bool = False
    has_rewrite: bool = False
    notes: str = ""


class DatasetManifest(EvalModel):
    """版本清单；draft 允许缺样本，frozen 必须齐全并通过校验。"""

    version: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    dataset_kind: Literal["e9", "engineering_fake"]
    status: Literal["draft", "frozen"]
    frozen_at: str = ""
    produced_by: str = ""
    usage: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)


class Dataset(EvalModel):
    """一个实验目录的内存视图；文件内容由 `dataset.load_dataset` 填入。"""

    manifest: DatasetManifest
    materials: list[MaterialAsset]
    cases: list[Case]
    qrels: list[Qrel]
    reviews: list[CaseReview]
    e2e_tasks: list[E2ETask]
    authorizations: list[str] = Field(default_factory=list)

    @property
    def assets_by_key(self) -> dict[str, MaterialAsset]:
        return {asset.material_key: asset for asset in self.materials}


class Experiment(EvalModel):
    """冻结候选与运行环境；同一 experiment_id 的重跑必须新建 attempt。"""

    experiment_id: str = Field(min_length=1)
    data_version: str = Field(min_length=1)
    index_digest: str = ""
    candidate_digest: str = ""
    code_commit: str = ""
    model: str = ""
    prompt_version: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    time_baseline: str = ""
    budget_id: str = ""


class Attempt(EvalModel):
    """一次（case × repeat × strategy × clue_mode）尝试；失败也保留。"""

    attempt_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    repeat: int = Field(ge=1, le=3)
    strategy: Strategy
    clue_mode: ClueMode
    status: AttemptStatus
    ranked_material_keys: list[str] = Field(default_factory=list)
    error: str = ""
    stage_latency_ms: dict[str, int] = Field(default_factory=dict)
    total_latency_ms: int | None = None
    cost_cny: float = 0.0
    unknown_cost_cny: float = 0.0
    result_ref: str = ""
    input_digest: str = ""

    @field_validator("ranked_material_keys")
    @classmethod
    def _dedupe_ranked(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
        return out


class RequestUsage(EvalModel):
    """一笔模型请求的预留 / 结算 / 未知占用记录。"""

    request_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    kind: RequestKind
    attempt_id: str = ""
    reserved_cny: float = Field(ge=0)
    status: RequestStatus
    estimated_cny: float = Field(ge=0, default=0.0)
    unknown_cny: float = Field(ge=0, default=0.0)
    upstream: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class HumanScore(EvalModel):
    """人工评分：uncertain 不计通过。"""

    attempt_id: str = Field(min_length=1)
    item: str = Field(min_length=1)
    verdict: Verdict
    evidence: str = ""
    reviewer: str = Field(min_length=1)


class Decision(EvalModel):
    """三类判定之一的结果；缺失项必须显式列出。"""

    gate: GateName
    status: GateStatus
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)

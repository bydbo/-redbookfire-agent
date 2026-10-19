"""数据结构契约：热点线索 / 素材 / 匹配结果 / 文案。

所有 LLM 的产出都必须先落成这里的结构，Workflow 才允许往下走。
这样模型说人话、程序看结构，两边不会互相污染。

实现说明（S1.1）：八个结构由 dataclass 升级为 Pydantic v2 模型，字段名、默认值、
`to_dict()` / `from_dict()` 的输入输出与升级前逐键一致；唯一新增的硬约束是
`MatchCandidate.reasons` 不允许为空（契约见 `docs/contracts/数据契约.md` 第七节与
`docs/contracts/openapi.yaml` 的 `MatchCandidate.reasons`）。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .util import clip01, now_iso, short_hash, slugify

# 爆点要素的类型表。这个分类是「热点 -> 素材」能不能对上的关键：
# 同一类型之间才比较，避免「羽毛球」和「热血」被算成相似。
ELEMENT_TYPES = (
    "ip",        # 人物/账号 IP：明星、顶流、企业家、素人标签
    "topic",     # 主题：羽毛球、露营、减脂餐
    "scene",     # 场景：球场、地铁、出租屋
    "visual",    # 画面元素：慢动作、特写、对比分屏
    "emotion",   # 情绪：反差、治愈、解压、共鸣
    "sound",     # 声音：BGM、原声、口播
    "conflict",  # 冲突/反差结构：身份反差、预期违背
    "format",    # 形式：跟练、同款、教程、测评
    "audience",  # 人群：打工人、宝妈、大学生
)

TYPE_LABELS = {
    "ip": "人物IP",
    "topic": "主题",
    "scene": "场景",
    "visual": "画面",
    "emotion": "情绪",
    "sound": "声音",
    "conflict": "反差结构",
    "format": "形式",
    "audience": "人群",
}

DEFAULT_TYPE_WEIGHTS = {
    "ip": 0.9,
    "topic": 1.0,
    "scene": 0.7,
    "visual": 0.55,
    "emotion": 0.6,
    "sound": 0.35,
    "conflict": 0.7,
    "format": 0.65,
    "audience": 0.5,
}

# 召回通道（《检索契约》§三）：字面通道 A 与向量通道 B。
RECALL_SOURCES = ("literal", "vector")


class SchemaError(ValueError):
    """结构不符合契约时抛出，Workflow 会据此重试或降级。"""


def _require(data: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in data or data[key] in (None, "", [], {}):
        raise SchemaError(f"{ctx} 缺少必填字段 `{key}`")
    return data[key]


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> list[str]:
    out = []
    for item in _as_list(value):
        if isinstance(item, dict):
            text = item.get("text") or item.get("value") or item.get("title")
            if text:
                out.append(str(text).strip())
        elif item is not None:
            text = str(item).strip()
            if text:
                out.append(text)
    return out


def _clean_str(value: Any) -> str:
    """宽松字符串归一：`None` / `False` / `0` 之类的假值一律视作空串。"""
    return str(value or "").strip()


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽松数值归一：解析不了或 NaN 时回落到默认值，不做上下界裁剪。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:
        return default
    return num


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _invalid(ctx: str, exc: ValidationError) -> SchemaError:
    """把 Pydantic 的校验失败折回本模块对外的统一异常。"""
    details = "；".join(
        f"{'.'.join(str(part) for part in err['loc']) or '<root>'}：{err['msg']}"
        for err in exc.errors()
    )
    return SchemaError(f"{ctx} 不符合契约：{details}")


class _Contract(BaseModel):
    """内存契约模型的公共配置：忽略未声明字段，保持可变更。"""

    model_config = ConfigDict(extra="ignore")


class Element(_Contract):
    """一个爆点要素：类型 + 取值 + 权重 + 置信度。"""

    type: str = "topic"
    value: str
    weight: float = 0.6
    confidence: float = 0.7
    evidence: str = ""

    @field_validator("type", mode="before")
    @classmethod
    def _norm_type(cls, value: Any) -> str:
        """类型必须是九类之一：未知值直接报错，由结构化输出的自修重试处理。

        静默回退成 `topic` 会同时踩两个坑：`topic` 权重最高（1.0），等于给错值加权重；
        模型输出跑偏也看不见——而 `docs/评测集.md` 的「幻觉」维度要求这类问题暴露出来。
        """
        text = _clean_str(value or "").lower()
        if text not in ELEMENT_TYPES:
            raise ValueError(f"要素类型必须是 {'/'.join(ELEMENT_TYPES)} 之一，收到 {value!r}")
        return text

    @field_validator("value", "evidence", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("weight", mode="before")
    @classmethod
    def _norm_weight(cls, value: Any) -> float:
        return clip01(value, 0.6)

    @field_validator("confidence", mode="before")
    @classmethod
    def _norm_confidence(cls, value: Any) -> float:
        return clip01(value, 0.7)

    @property
    def label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def score_weight(self) -> float:
        """要素在打分里的实际份量 = 重要性 x 置信度。"""
        return max(0.05, self.weight * self.confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "value": self.value,
            "weight": round(self.weight, 3),
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Element:
        if isinstance(data, str):
            data = {"type": "topic", "value": data}
        if not isinstance(data, dict):
            raise SchemaError(f"要素必须是对象或字符串，收到 {type(data).__name__}")
        try:
            return cls(
                type=data.get("type", "topic"),
                value=_require(data, "value", "要素"),
                weight=data.get("weight", 0.6),
                confidence=data.get("confidence", 0.7),
                evidence=data.get("evidence", ""),
            )
        except ValidationError as exc:
            raise _invalid("要素", exc) from exc


class Mechanism(_Contract):
    """为什么这个热点能火的一条机制，例如「反差」「参与门槛低」。"""

    name: str
    explain: str = ""

    @field_validator("name", "explain", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "explain": self.explain}

    @classmethod
    def from_dict(cls, data: Any) -> Mechanism:
        if isinstance(data, str):
            return cls(name=data.strip(), explain="")
        if not isinstance(data, dict):
            raise SchemaError("mechanism 必须是对象或字符串")
        name = data.get("name") or data.get("type") or data.get("title")
        if not name:
            raise SchemaError("mechanism 缺少 name")
        try:
            return cls(name=str(name).strip(),
                       explain=str(data.get("explain") or data.get("why") or "").strip())
        except ValidationError as exc:
            raise _invalid("mechanism", exc) from exc


class HotspotClue(_Contract):
    """热点线索：一条热点被拆解后的可迁移结论。"""

    hotspot_raw: str
    why_it_works: list[str] = Field(default_factory=list)
    mechanisms: list[Mechanism] = Field(default_factory=list)
    elements: list[Element] = Field(default_factory=list)
    match_keywords: list[str] = Field(default_factory=list)
    audience: dict[str, Any] = Field(default_factory=dict)
    borrow_angles: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    created_at: str = Field(default_factory=now_iso)
    hotspot_key: str = ""

    @field_validator("hotspot_raw", "provider", "model", "created_at", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("hotspot_key", mode="before")
    @classmethod
    def _norm_key(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("why_it_works", "match_keywords", "borrow_angles", "risk_notes",
                     mode="before")
    @classmethod
    def _norm_str_list(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @field_validator("mechanisms", "elements", mode="before")
    @classmethod
    def _norm_model_list(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("audience", mode="before")
    @classmethod
    def _norm_audience(cls, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        text = _clean_str(value)
        return {"core": text} if text else {}

    @model_validator(mode="after")
    def _fill_hotspot_key(self) -> HotspotClue:
        if not self.hotspot_key:
            self.hotspot_key = slugify(self.hotspot_raw, max_len=20, fallback="hotspot")
        return self

    @property
    def element_values(self) -> list[str]:
        return [e.value for e in self.elements]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hotspot_raw": self.hotspot_raw,
            "hotspot_key": self.hotspot_key,
            "why_it_works": self.why_it_works,
            "mechanisms": [m.to_dict() for m in self.mechanisms],
            "elements": [e.to_dict() for e in self.elements],
            "match_keywords": self.match_keywords,
            "audience": self.audience,
            "borrow_angles": self.borrow_angles,
            "risk_notes": self.risk_notes,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], hotspot_raw: str = "", provider: str = "", model: str = "") -> HotspotClue:
        if not isinstance(data, dict):
            raise SchemaError("热点线索必须是一个 JSON 对象")

        raw = str(data.get("hotspot_raw") or hotspot_raw or "").strip()
        if not raw:
            raise SchemaError("缺少 hotspot_raw")

        elements_raw = _require(data, "elements", "热点线索")
        elements = [Element.from_dict(item) for item in _as_list(elements_raw)]
        elements = [e for e in elements if e.value]
        if not elements:
            raise SchemaError("热点线索至少要有一个可用要素 (elements)")

        audience = data.get("audience") or {}
        if isinstance(audience, str):
            audience = {"core": audience.strip()}
        if not isinstance(audience, dict):
            audience = {}

        mechanism_raw = data.get("mechanisms") or data.get("why_it_works") or []
        mechanisms = []
        if isinstance(mechanism_raw, list) and mechanism_raw and isinstance(mechanism_raw[0], dict):
            mechanisms = [Mechanism.from_dict(m) for m in mechanism_raw]
        why = _as_str_list(data.get("why_it_works")) or [f"{m.name}：{m.explain}".strip("：") for m in mechanisms]

        keywords = _as_str_list(data.get("match_keywords"))
        if not keywords:
            keywords = [e.value for e in elements]

        distinct = []
        seen = set()
        for item in keywords:
            key = item.lower()
            if key not in seen:
                seen.add(key)
                distinct.append(item)

        try:
            return cls(
                hotspot_raw=raw,
                why_it_works=why,
                mechanisms=mechanisms,
                elements=elements,
                match_keywords=distinct[:20],
                audience=audience,
                borrow_angles=_as_str_list(data.get("borrow_angles")),
                risk_notes=_as_str_list(data.get("risk_notes")),
                provider=provider,
                model=model,
                hotspot_key=slugify(str(data.get("hotspot_key") or raw), max_len=20, fallback="hotspot"),
            )
        except ValidationError as exc:
            raise _invalid("热点线索", exc) from exc


class Material(_Contract):
    """素材仓库里的一条素材。"""

    id: str
    path: str
    type: str = "video"          # video | image | text
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    elements: list[Element] = Field(default_factory=list)
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    size_bytes: int = 0
    mtime: float = 0.0
    source: str = "filename"     # sidecar | vision | filename | legacy
    keyframes: list[str] = Field(default_factory=list)
    indexed_at: str = Field(default_factory=now_iso)
    fingerprint: str = ""

    @field_validator("id", "path", "type", "title", "description", "source",
                     "indexed_at", "fingerprint", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("tags", "keyframes", mode="before")
    @classmethod
    def _norm_str_list(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @field_validator("elements", mode="before")
    @classmethod
    def _norm_model_list(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("duration_s", "mtime", mode="before")
    @classmethod
    def _norm_float(cls, value: Any) -> float:
        return _as_float(value)

    @field_validator("width", "height", "size_bytes", mode="before")
    @classmethod
    def _norm_int(cls, value: Any) -> int:
        return _as_int(value)

    @property
    def aspect_ratio(self) -> float:
        return (self.width / self.height) if self.width and self.height else 0.0

    @property
    def is_vertical(self) -> bool:
        return 0.0 < self.aspect_ratio < 0.95

    @property
    def quality_score(self) -> float:
        """画质适配度：竖屏 + 高清更适合小红书。"""
        if not self.width or not self.height:
            return 0.4
        score = 0.5
        if max(self.width, self.height) >= 1280:
            score += 0.25
        elif max(self.width, self.height) >= 720:
            score += 0.12
        if self.is_vertical:
            score += 0.15
        if self.type == "video" and 4 <= self.duration_s <= 60:
            score += 0.1
        return min(1.0, score)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "tags": self.tags,
            "elements": [e.to_dict() for e in self.elements],
            "duration_s": round(self.duration_s, 2),
            "width": self.width,
            "height": self.height,
            "has_audio": self.has_audio,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "source": self.source,
            "keyframes": self.keyframes,
            "indexed_at": self.indexed_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Material:
        try:
            material = cls(
                id=str(data.get("id") or ""),
                path=str(data.get("path") or ""),
                type=str(data.get("type") or "video"),
                title=str(data.get("title") or ""),
                description=str(data.get("description") or ""),
                tags=[str(t) for t in _as_str_list(data.get("tags"))],
                duration_s=float(data.get("duration_s") or 0.0),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
                has_audio=bool(data.get("has_audio")),
                size_bytes=int(data.get("size_bytes") or 0),
                mtime=float(data.get("mtime") or 0.0),
                source=str(data.get("source") or "filename"),
                keyframes=[str(k) for k in _as_list(data.get("keyframes"))],
                indexed_at=str(data.get("indexed_at") or now_iso()),
                fingerprint=str(data.get("fingerprint") or ""),
            )
        except ValidationError as exc:
            raise _invalid("素材", exc) from exc
        material.elements = [Element.from_dict(item) for item in _as_list(data.get("elements"))]
        return material

    @staticmethod
    def make_id(rel_path: str) -> str:
        return "m_" + short_hash(rel_path, 10)


class ElementHit(_Contract):
    """一条匹配记录：线索要素被哪条素材信息命中。"""

    element_type: str
    clue_value: str
    hit_value: str = ""
    similarity: float = 0.0
    contribution: float = 0.0

    @field_validator("element_type", "clue_value", "hit_value", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("similarity", "contribution", mode="before")
    @classmethod
    def _norm_float(cls, value: Any) -> float:
        return _as_float(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_type": self.element_type,
            "clue_value": self.clue_value,
            "hit_value": self.hit_value,
            "similarity": round(self.similarity, 3),
            "contribution": round(self.contribution, 3),
        }


class MatchCandidate(_Contract):
    """一条候选素材及其匹配解释。

    `reasons` 是契约硬约束：不允许出现无理由候选。由于打分与解释是两步，
    中间态必须走 `MatchCandidate.draft(...)` 构造，解释补齐后再 `finalize()`。
    """

    material_id: str
    material: Material
    score: float = 0.0
    recall_sources: list[str] = Field(default_factory=list)
    hits: list[ElementHit] = Field(default_factory=list)
    missing: list[Element] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    usage: str = ""
    rank: int = 0

    @field_validator("material_id", "usage", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("score", mode="before")
    @classmethod
    def _norm_score(cls, value: Any) -> float:
        return _as_float(value)

    @field_validator("rank", mode="before")
    @classmethod
    def _norm_rank(cls, value: Any) -> int:
        return _as_int(value)

    @field_validator("recall_sources", mode="before")
    @classmethod
    def _norm_recall_sources(cls, value: Any) -> list[str]:
        """只保留契约允许的召回通道，去重后按固定顺序（literal → vector）排列。"""
        present = set(_as_str_list(value))
        return [name for name in RECALL_SOURCES if name in present]

    @field_validator("hits", "missing", mode="before")
    @classmethod
    def _norm_model_list(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("reasons", mode="before")
    @classmethod
    def _norm_reasons(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @model_validator(mode="after")
    def _require_reasons(self, info: ValidationInfo) -> MatchCandidate:
        if not self.reasons and not (info.context or {}).get("allow_empty_reasons"):
            raise ValueError("候选素材缺少 reasons：契约不允许无理由候选")
        return self

    @classmethod
    def draft(cls, **kwargs: Any) -> MatchCandidate:
        """中间态构造入口：此时 `reasons` 允许为空，等解释补齐后再 `finalize()`。"""
        return cls.model_validate(kwargs, context={"allow_empty_reasons": True})

    def finalize(self) -> MatchCandidate:
        """终态校验：理由为空即视为未完成，不能流向报告与落库。"""
        if not self.reasons:
            raise SchemaError(f"候选素材 {self.material_id or self.material.id} 缺少 reasons")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "score": round(self.score, 3),
            "rank": self.rank,
            "recall_sources": list(self.recall_sources),
            "reasons": self.reasons,
            "usage": self.usage,
            "hits": [h.to_dict() for h in self.hits],
            "missing": [e.to_dict() for e in self.missing],
            "material": self.material.to_dict(),
        }


class Coverage(_Contract):
    """线索覆盖度：哪些爆点要素有素材，哪些是缺口。"""

    covered: list[Element] = Field(default_factory=list)
    gaps: list[Element] = Field(default_factory=list)
    ratio: float = 0.0

    @field_validator("covered", "gaps", mode="before")
    @classmethod
    def _norm_model_list(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("ratio", mode="before")
    @classmethod
    def _norm_ratio(cls, value: Any) -> float:
        return _as_float(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ratio": round(self.ratio, 3),
            "covered": [e.to_dict() for e in self.covered],
            "gaps": [e.to_dict() for e in self.gaps],
        }


class Draft(_Contract):
    """一版初步文案。"""

    material_id: str = ""
    hotspot_key: str = ""
    titles: list[dict[str, Any]] = Field(default_factory=list)   # [{text, style}]
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    cover_text: str = ""
    first_3s: str = ""
    shot_list: list[str] = Field(default_factory=list)
    compliance_notes: list[str] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    created_at: str = Field(default_factory=now_iso)

    XHS_TITLE_LIMIT: ClassVar[int] = 20

    @field_validator("material_id", "hotspot_key", "body", "cover_text", "first_3s",
                     "provider", "model", "created_at", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("tags", "shot_list", "compliance_notes", mode="before")
    @classmethod
    def _norm_str_list(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @field_validator("titles", mode="before")
    @classmethod
    def _norm_titles(cls, value: Any) -> list[dict[str, Any]]:
        return _as_list(value)

    def title_warnings(self) -> list[str]:
        out = []
        for item in self.titles:
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            if len(text) > self.XHS_TITLE_LIMIT:
                out.append(f"标题超 {self.XHS_TITLE_LIMIT} 字：{text[:30]}…")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "hotspot_key": self.hotspot_key,
            "titles": self.titles,
            "body": self.body,
            "tags": self.tags,
            "cover_text": self.cover_text,
            "first_3s": self.first_3s,
            "shot_list": self.shot_list,
            "compliance_notes": self.compliance_notes,
            "provider": self.provider,
            "model": self.model,
            "created_at": self.created_at,
            "title_warnings": self.title_warnings(),
        }

    def to_markdown(self) -> str:
        lines = ["### 标题备选"]
        for item in self.titles:
            if isinstance(item, dict):
                lines.append(f"- {item.get('text', '')}（{item.get('style', '未标注')} / {len(item.get('text', ''))} 字）")
            else:
                lines.append(f"- {item}")
        if self.cover_text:
            lines += ["", "### 封面文字", self.cover_text]
        if self.first_3s:
            lines += ["", "### 开头 3 秒", self.first_3s]
        lines += ["", "### 正文", self.body, "", "### 话题标签", " ".join(self.tags)]
        if self.shot_list:
            lines += ["", "### 剪辑顺序建议"]
            lines += [f"{i + 1}. {item}" for i, item in enumerate(self.shot_list)]
        if self.compliance_notes:
            lines += ["", "### 合规提醒"]
            lines += [f"- {item}" for item in self.compliance_notes]
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: dict[str, Any], material_id: str = "", hotspot_key: str = "",
                  provider: str = "", model: str = "") -> Draft:
        if not isinstance(data, dict):
            raise SchemaError("文案必须是一个 JSON 对象")
        titles_raw = _as_list(data.get("titles"))
        titles = []
        for item in titles_raw:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("title") or "").strip()
                if text:
                    titles.append({"text": text, "style": str(item.get("style") or "未标注").strip()})
            elif item is not None and str(item).strip():
                titles.append({"text": str(item).strip(), "style": "未标注"})
        body = str(data.get("body") or data.get("content") or "").strip()
        if not body:
            raise SchemaError("文案缺少 body")
        tags = []
        for tag in _as_str_list(data.get("tags")):
            tag = tag if tag.startswith("#") else f"#{tag}"
            tags.append(tag)
        try:
            return cls(
                material_id=str(data.get("material_id") or material_id),
                hotspot_key=str(data.get("hotspot_key") or hotspot_key),
                titles=titles[:5],
                body=body,
                tags=tags[:12],
                cover_text=str(data.get("cover_text") or "").strip(),
                first_3s=str(data.get("first_3s") or "").strip(),
                shot_list=_as_str_list(data.get("shot_list")),
                compliance_notes=_as_str_list(data.get("compliance_notes")),
                provider=provider,
                model=model,
            )
        except ValidationError as exc:
            raise _invalid("文案", exc) from exc


# ---------------------------------------------------------------------------
# E7 第六批（S7.24 / S7.25）：上下文事实、提案与产物版本
# ---------------------------------------------------------------------------
# 口径：这里的结构是**内存契约**，落库形状见《数据契约》§3.8 / §3.9。
# 模型输出先落成 `ContextDelta`，经服务端证据 / 归属 / 作用域校验后才允许写进会话快照；
# 模型给的 `fact_id`、`status`、`created_version` 一律不采信——由服务端生成。

FACT_KINDS: tuple[str, ...] = (
    "goal",         # 目标：这条任务要达成什么
    "constraint",   # 限制：长度、字数、不得出现的内容
    "preference",   # 偏好：语气、风格、表达习惯
    "audience",     # 受众：给谁看
    "format",       # 形式：体裁、结构、平台要求
)
FACT_STATUSES: tuple[str, ...] = (
    "active",
    "pending_confirmation",
    "superseded",
    "retracted",
)
FACT_SCOPES: tuple[str, ...] = ("session", "topic", "artifact")
CONTEXT_ACTIONS: tuple[str, ...] = (
    "confirm", "reject", "retract", "replace", "reset_topic",
    "select_object", "recover", "complete_recovery",
)
OBJECT_TYPES: tuple[str, ...] = ("draft", "clue")
VERSION_STATUSES: tuple[str, ...] = (
    "pending",
    "succeeded",
    "failed",
    "refused",
    "interrupted",
)
# 单条事实 / 提案的长度上限：状态要能装进请求预算，不能变成第二份正文
MAX_FACT_CHARS = 200
# 作用域内最多保留多少条生效事实（超出时归档最旧的非 session 项，session 项不自动丢）
MAX_FACTS_PER_SCOPE = 40


class ContextFact(BaseModel):
    """一条会话事实（S7.24）。

    `evidence` 必须是用户原文的真实片段；`source_ref` 至少带 `message_id` 或 `action_id`。
    `status=active` 且缺证据时**不允许**构造——证据是生效的必要条件，不是装饰。
    """

    model_config = ConfigDict(extra="ignore")

    fact_id: str
    kind: str
    value: str
    scope: str = "topic"
    scope_id: str = ""
    status: str = "active"
    source_ref: dict[str, Any] = Field(default_factory=dict)
    evidence: str = ""
    created_version: int = 0
    replaces: str | None = None
    # E7CTX-04（v2）：多项替代时记录全部被替换的旧事实 id；单项替代与 `replaces` 一致
    replaces_fact_ids: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=now_iso)

    @field_validator("replaces_fact_ids", mode="before")
    @classmethod
    def _norm_replace_ids(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @field_validator("fact_id", "value", "scope_id", "evidence", "updated_at", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("replaces", mode="before")
    @classmethod
    def _norm_optional(cls, value: Any) -> str | None:
        text = _clean_str(value)
        return text or None

    @field_validator("source_ref", mode="before")
    @classmethod
    def _norm_source(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @field_validator("created_version", mode="before")
    @classmethod
    def _norm_version(cls, value: Any) -> int:
        return max(0, _as_int(value, 0))

    @model_validator(mode="after")
    def _check(self) -> ContextFact:
        if not self.fact_id:
            raise ValueError("fact_id 不能为空")
        if self.kind not in FACT_KINDS:
            raise ValueError(f"kind 只能是 {' / '.join(FACT_KINDS)}")
        if self.status not in FACT_STATUSES:
            raise ValueError(f"status 只能是 {' / '.join(FACT_STATUSES)}")
        if self.scope not in FACT_SCOPES:
            raise ValueError(f"scope 只能是 {' / '.join(FACT_SCOPES)}")
        if not self.value:
            raise ValueError("value 不能为空")
        if len(self.value) > MAX_FACT_CHARS:
            raise ValueError(f"value 不能超过 {MAX_FACT_CHARS} 字")
        if self.status == "active" and not self.evidence:
            raise ValueError("生效事实必须有 evidence（用户原文片段）")
        if self.status == "active" and not (
            self.source_ref.get("message_id") or self.source_ref.get("action_id")
        ):
            raise ValueError("生效事实必须有来源（message_id / action_id）")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind,
            "value": self.value,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "status": self.status,
            "source_ref": dict(self.source_ref),
            "evidence": self.evidence,
            "created_version": self.created_version,
            "replaces": self.replaces,
            "replaces_fact_ids": list(self.replaces_fact_ids),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextFact:
        if not isinstance(data, dict):
            raise SchemaError("事实必须是一个 JSON 对象")
        try:
            return cls(**data)
        except ValidationError as exc:
            raise _invalid("事实", exc) from exc


class ContextProposal(BaseModel):
    """一条待确认提案（S7.24）：推断、歧义或替换意图不明的冲突。

    `proposal_id` 由服务端生成；模型只能给内容与理由。提案**不生效**，确认后才转成事实。
    """

    model_config = ConfigDict(extra="ignore")

    proposal_id: str
    kind: str
    value: str
    scope: str = "topic"
    scope_id: str = ""
    reason: str = ""
    evidence: str = ""
    source_ref: dict[str, Any] = Field(default_factory=dict)
    conflicts_with: list[str] = Field(default_factory=list)
    created_version: int = 0
    # E7CTX-04 / v2（数据契约 §3.8）：提案保存拟执行操作与替代关系——
    # 确认 replace 时按 `replaces_fact_ids` 原子替换旧项；旧提案兼容读取：
    # 有 conflicts_with 映射为 replace，否则默认 add。
    operation: str = "add"
    replaces_fact_ids: list[str] = Field(default_factory=list)
    target_object_ref: dict[str, Any] = Field(default_factory=dict)
    source_message_ids: list[str] = Field(default_factory=list)

    @field_validator("proposal_id", "value", "scope_id", "reason", "evidence", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @field_validator("operation", mode="before")
    @classmethod
    def _norm_operation(cls, value: Any) -> str:
        text = _clean_str(value) or "add"
        if text not in ("add", "replace", "retract", "reset_topic", "select_object"):
            raise ValueError("operation 只能是 add / replace / retract / reset_topic / select_object")
        return text

    @field_validator("source_ref", mode="before")
    @classmethod
    def _norm_source(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @field_validator("conflicts_with", mode="before")
    @classmethod
    def _norm_conflicts(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @field_validator("created_version", mode="before")
    @classmethod
    def _norm_version(cls, value: Any) -> int:
        return max(0, _as_int(value, 0))

    @model_validator(mode="after")
    def _check(self) -> ContextProposal:
        if not self.proposal_id:
            raise ValueError("proposal_id 不能为空")
        if self.kind not in FACT_KINDS:
            raise ValueError(f"kind 只能是 {' / '.join(FACT_KINDS)}")
        if self.scope not in FACT_SCOPES:
            raise ValueError(f"scope 只能是 {' / '.join(FACT_SCOPES)}")
        if not self.value:
            raise ValueError("value 不能为空")
        if len(self.value) > MAX_FACT_CHARS:
            raise ValueError(f"value 不能超过 {MAX_FACT_CHARS} 字")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "value": self.value,
            "scope": self.scope,
            "scope_id": self.scope_id,
            "reason": self.reason,
            "evidence": self.evidence,
            "source_ref": dict(self.source_ref),
            "conflicts_with": list(self.conflicts_with),
            "created_version": self.created_version,
            "operation": self.operation,
            "replaces_fact_ids": list(self.replaces_fact_ids),
            "target_object_ref": dict(self.target_object_ref),
            "source_message_ids": list(self.source_message_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextProposal:
        if not isinstance(data, dict):
            raise SchemaError("提案必须是一个 JSON 对象")
        payload = dict(data)
        # 旧提案兼容读取：没有 operation 时按 conflicts_with 推导替代关系（数据契约 §3.8）
        if "operation" not in payload:
            conflicts = _as_str_list(payload.get("conflicts_with"))
            payload["operation"] = "replace" if conflicts else "add"
            payload.setdefault("replaces_fact_ids", conflicts)
        try:
            return cls(**payload)
        except ValidationError as exc:
            raise _invalid("提案", exc) from exc


class ContextSnapshot(BaseModel):
    """会话状态快照（`chat_sessions.context_state` 的内存投影）。"""

    model_config = ConfigDict(extra="ignore")

    state_version: int = 0
    topic_id: str | None = None
    facts: list[ContextFact] = Field(default_factory=list)
    proposals: list[ContextProposal] = Field(default_factory=list)
    object_refs: list[dict[str, Any]] = Field(default_factory=list)
    archived: list[ContextFact] = Field(default_factory=list)
    recovery_status: str = "complete"
    recovery_reason: str = ""

    @field_validator("state_version", mode="before")
    @classmethod
    def _norm_version(cls, value: Any) -> int:
        return max(0, _as_int(value, 0))

    @field_validator("facts", "proposals", "archived", mode="before")
    @classmethod
    def _norm_items(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("object_refs", mode="before")
    @classmethod
    def _norm_refs(cls, value: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in _as_list(value) if isinstance(item, dict)]

    @model_validator(mode="after")
    def _check(self) -> ContextSnapshot:
        if self.recovery_status not in ("complete", "partial"):
            raise ValueError("recovery_status 只能是 complete / partial")
        return self

    def active(self, *, scope_id: str | None = None, kind: str | None = None) -> list[ContextFact]:
        """生效事实（可按作用域对象 / 类型过滤；`session` 作用域始终有效）。"""
        out = []
        for fact in self.facts:
            if fact.status != "active":
                continue
            if scope_id is not None and fact.scope != "session" \
                    and fact.scope_id and fact.scope_id != scope_id:
                continue
            if kind is not None and fact.kind != kind:
                continue
            out.append(fact)
        return out

    def pending(self) -> list[ContextProposal]:
        return list(self.proposals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "facts": [item.to_dict() for item in self.facts],
            "proposals": [item.to_dict() for item in self.proposals],
            "object_refs": [dict(item) for item in self.object_refs],
            "archived": [item.to_dict() for item in self.archived],
            "recovery_status": self.recovery_status,
            "recovery_reason": self.recovery_reason,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, state_version: int = 0,
        topic_id: str | None = None, recovery_status: str = "complete",
    ) -> ContextSnapshot:
        """从库里读回；坏数据不抛错——**降级成 partial**（旧会话 / 手工改库都要能读）。"""
        raw = dict(data or {})
        facts: list[ContextFact] = []
        proposals: list[ContextProposal] = []
        try:
            facts = [ContextFact.from_dict(item) for item in _as_list(raw.get("facts"))]
            proposals = [
                ContextProposal.from_dict(item) for item in _as_list(raw.get("proposals"))
            ]
        except SchemaError:
            return cls(
                state_version=state_version, topic_id=topic_id,
                recovery_status="partial",
                recovery_reason="状态快照中有无法解析的条目",
            )
        archived: list[ContextFact] = []
        try:
            archived = [
                ContextFact.from_dict(item) for item in _as_list(raw.get("archived"))
            ]
        except SchemaError:
            archived = []
        return cls(
            state_version=state_version,
            topic_id=topic_id,
            facts=facts,
            proposals=proposals,
            object_refs=[dict(item) for item in _as_list(raw.get("object_refs"))
                         if isinstance(item, dict)],
            archived=archived,
            recovery_status=recovery_status,
            recovery_reason=_clean_str(raw.get("recovery_reason")),
        )


class ContextOperation(BaseModel):
    """抽取增量里的一条操作（`add` / `retract` / `replace`），尚未经服务端校验。"""

    model_config = ConfigDict(extra="ignore")

    op: str
    kind: str = ""
    value: str = ""
    scope: str = "topic"
    scope_id: str = ""
    target_fact_id: str = ""
    evidence: str = ""
    reason: str = ""

    @field_validator("op", "kind", "value", "scope", "scope_id", "target_fact_id",
                     "evidence", "reason", mode="before")
    @classmethod
    def _norm_text(cls, value: Any) -> str:
        return _clean_str(value)

    @model_validator(mode="after")
    def _check(self) -> ContextOperation:
        if self.op not in ("add", "retract", "replace"):
            raise ValueError("op 只能是 add / retract / replace")
        if self.op in ("add", "replace") and not self.value:
            raise ValueError(f"{self.op} 需要 value")
        if self.op == "add" and self.kind not in FACT_KINDS:
            raise ValueError(f"add 的 kind 只能是 {' / '.join(FACT_KINDS)}")
        if self.op in ("retract", "replace") and not self.target_fact_id:
            raise ValueError(f"{self.op} 需要 target_fact_id")
        if self.scope not in FACT_SCOPES:
            raise ValueError(f"scope 只能是 {' / '.join(FACT_SCOPES)}")
        return self


class ContextDelta(BaseModel):
    """一次抽取的完整增量：操作 + 待确认提案（提案内容未经服务端编号）。"""

    model_config = ConfigDict(extra="ignore")

    operations: list[ContextOperation] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("operations", mode="before")
    @classmethod
    def _norm_ops(cls, value: Any) -> list[Any]:
        return _as_list(value)

    @field_validator("proposals", mode="before")
    @classmethod
    def _norm_proposals(cls, value: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in _as_list(value) if isinstance(item, dict)]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextDelta:
        if not isinstance(data, dict):
            raise SchemaError("状态增量必须是一个 JSON 对象")
        try:
            return cls(**data)
        except ValidationError as exc:
            raise _invalid("状态增量", exc) from exc

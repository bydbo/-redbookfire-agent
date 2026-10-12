"""对话可调用的工具（S7.3 起；S7.7 加只读三件，S7.7b 加拆解预览，S7.8 加改写）。

用途：把「对话层能做的事」写成工具——跑完整分析（第一批）、解析图片 / 查素材库 / 读历史运行
      （S7.7 只读三件）、预览爆点要素与缺口（S7.7b）、改写文案（S7.8）。
输入：`ToolContext`（配置 / 会话 / 投递器 / 文本模型 caller / 可注入的连接池与 sleep）+ 工具参数 dict。
输出：`ToolOutcome`（status / payload / steps / error / prompt_versions）——payload 既喂回给模型，
      也落进 `chat_messages.tool_calls`。

口径：

- **工具定义（`TOOLS`）是唯一来源，执行分派（`HANDLERS`）也在本文件**（S7.7a）：给模型的
  function schema 与 name → handler 映射都集中在这里，`workflows/chat.py` 里不出现任何具体
  工具名——「新增一个工具只改 `chat_tools.py` 一处」。prompt 契约管的是 `prompts/` 下的
  prompt 正文；工具的 name/description/参数属于 API 协议，随代码走，不抄一份进 prompt；
- **只读的有哪些**：`parse_image_clue`（图片只在内存里变成模型输入，不落盘不入库）、
  `search_materials`（只检索）、`read_run`（只读、不花钱）、`preview_element_coverage`
  （不调 embedding、不写稿、不落 runs）。有副作用的只有 `run_hotspot_analysis`（投 job）
  与 `revise_draft`（花一次模型钱，但**不写库**——run 的初稿快照不动）；
- **一轮内不重复付费**：`ToolContext.cache` 按「热点原文」缓存拆解结果、按「附件路径」缓存
  图片解析结果，所以「先预览再检索」「先解析图片再跑分析」都只付一次模型钱；
- **`run_id` 可省**（`read_run` / `revise_draft`）：不传就落到本会话**最近一次**运行产出的
  那个（`services/chat.py::latest_run_id`）——装配给模型的历史只有消息正文，工具返回值里的 id
  它看不到，用户说「上次那批」「刚才那版」时只能由服务端兜底（S7.9 冒烟实测到这条）；
- 有图片附件时先走 S6.6 的 `parse_image_clue`（多模态），把解析出的线索作为**预置线索**
  随 `submit_analysis(clues=...)` 提交——拆解节点短路，不重复付费（S6.7）；
- 分析在 Celery worker 跑，本工具只等结果：轮询 `runs`（ADR 0011：库是权威源），
  上限 `[queue].task_time_limit_s`；同一 session 里轮询前必须 `expire_all()`，否则读到旧对象；
- **成本记账**：文本模型调用的成本取 `StructuredCaller.records` 的增量，写进
  `payload["cost_cny"]`（由对话图累加进本轮成本）；多模态图片解析仍按 S6.6 口径**不计成本**；
  `read_run` 不花钱；
- 参数不合法 / 上游失败一律返回 `status=failed` + 一句能直接回给用户的原因，不抛异常
  （抛出去会触发节点重试，可能重复付费）。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.clue import TASK_ID as CLUE_TASK
from ..agents.clue import extract_clue
from ..agents.revise import TASK_ID as REVISE_TASK
from ..agents.revise import ReviseOutcome
from ..agents.revise import revise_draft as revise_agent
from ..config import AppConfig
from ..core.errors import ApiError, BadRequestError
from ..schemas import TYPE_LABELS, Element, HotspotClue, SchemaError
from ..services.chat import latest_run_id, resolve_attachment
from ..services.image_clue import TASK_ID as IMAGE_CLUE_TASK
from ..services.image_clue import ImageClueParse, parse_image_clue
from ..services.retrieval import literal_only_candidates, retrieve_candidates
from ..services.runs import load_job, load_run, submit_analysis
from ..tools.embedding import EmbeddingError
from ..tools.gaps import gap_advice
from ..tools.llm import LLMError, StructuredCaller
from ..tools.vision import sniff_image_mime

TOOL_RUN_ANALYSIS = "run_hotspot_analysis"
TOOL_PARSE_IMAGE = "parse_image_clue"
TOOL_SEARCH_MATERIALS = "search_materials"
TOOL_READ_RUN = "read_run"
TOOL_PREVIEW_COVERAGE = "preview_element_coverage"
TOOL_REVISE_DRAFT = "revise_draft"

# 运行终态：出现在这里就是"这一批跑完了"（与 `tasks/analysis.py` 同一口径）
RUN_TERMINAL = ("succeeded", "failed")

# 热点原文上限（与 `POST /api/analyze` 的 AnalyzeRequest 同口径）
MAX_HOTSPOT_CHARS = 500
# 修改要求上限（与热点原文同口径；模型看上下文有限，太长反而抓不到重点）
MAX_REQUEST_CHARS = 500
MAX_TOPK = 20
# 回给模型的候选条数上限：再多也是噪声，且会挤占对话上下文
CANDIDATE_PREVIEW_LIMIT = 8
TAG_PREVIEW_LIMIT = 6
HIT_PREVIEW_LIMIT = 3

# 给模型的工具清单（OpenAI 兼容的 function schema）。**唯一来源**，别在别处再抄一份。
# description 里写"什么时候用它"——prompt 的 03 Constraints 只写"能不能调、边界在哪"（prompt 契约 §一）。
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_RUN_ANALYSIS,
            "description": (
                "把一个热点跑一遍完整分析：拆解爆点要素 → 在创作者本地的素材库里检索可蹭的素材 → "
                "给出命中理由与覆盖缺口 → 写一版文案初稿。用户说「蹭一下这个热点」「看看有没有"
                "素材」「写一版文案」时调用它。用户只发了图片、没写热点文字时，热点参数留空即可，"
                "工具会用图片自己解析出描述。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotspot": {
                        "type": "string",
                        "description": "热点原文（1–500 字）。整理用户原话即可，不要改写成标题。"
                                       "只有图片时留空。",
                    },
                    "topk": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "每个热点返回几条候选素材，默认 5。用户要「多看几条」时再改。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_PARSE_IMAGE,
            "description": (
                "把用户这一轮发来的图片拆成「热点描述 + 爆点要素」。**只解析图片**：不检索素材、"
                "不写文案、不落库、不产生运行记录。用户只丢了一张图、想先看看图里是什么热点，"
                "或者你想先确认图片内容再决定下一步时用它。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_SEARCH_MATERIALS,
            "description": (
                "在创作者本地的素材库里检索能蹭这条热点的素材，返回爆点要素、要素覆盖率与候选"
                "素材（含命中理由与建议用法）。**只检索、不写文案、不产生运行记录**；用户问"
                "「有没有能用的素材」「这条热点我能蹭吗」时用它。热点原文由你整理后传入。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotspot": {
                        "type": "string",
                        "description": "热点原文（1–500 字）。整理用户原话即可，不要改写成标题。",
                    },
                    "topk": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "返回几条候选素材，默认 5。用户要「多看几条」时再改。",
                    },
                },
                "required": ["hotspot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_READ_RUN,
            "description": (
                "读回一次**已完成**的运行结果（按 run_id）：每条热点的爆点要素、要素覆盖率、"
                "候选素材条数、首选素材与初稿标题。它不重新分析、不检索、不花钱；用户追问"
                "「刚才那次」「上次那批」的结果细节时用它。**run_id 可以不传**——不传就用"
                "这个会话里最近一次运行的结果（用户说「上次那批」时正是这种情况）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "要读回的运行 id（uuid）。可取之前分析的返回值；"
                                       "用户指的是「上次那批」时留空即可，不要自己编。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_PREVIEW_COVERAGE,
            "description": (
                "只做要素拆解与覆盖估算：把一条热点拆成九类爆点要素，再按你本地素材库里"
                "「已经沾边的素材」估算要素覆盖率、指出缺口并给出补拍建议。**这一步不产出候选"
                "素材、不写文案、不产生运行记录**；用户想先看看这条热点由什么构成、缺哪些画面，"
                "或者想省一次完整分析的成本时用它。要看具体候选素材请改用 search_materials，"
                "要完整分析和文案请用 run_hotspot_analysis。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hotspot": {
                        "type": "string",
                        "description": "热点原文（1–500 字）。整理用户原话即可，不要改写成标题。",
                    },
                },
                "required": ["hotspot"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_REVISE_DRAFT,
            "description": (
                "按用户的要求改写**某次运行已经产出的文案初稿**（改结构、口吻、详略、角度）："
                "不重新分析、不检索素材、**不覆盖原结果**——改后的文案只出现在这次回复里，"
                "由你讲给用户。用户在初稿基础上提修改意见（「标题再狠一点」「正文写短些」"
                "「换成避坑角度」）时用它。要求越界（要写他人姓名/肖像、要编造素材里没有的事）时"
                "工具会拒绝并给出理由，你把这个理由如实讲给用户，不要替他换个说法再来一次。"
                "**run_id 可以不传**：不传就改这个会话里最近一次运行产出的初稿"
                "（用户说「刚才那版」时正是这种情况）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "要改写的那次运行的 run_id（uuid）。取自之前分析的"
                                       "返回值；用户指的是「刚才那版」时留空即可，不要自己编。",
                    },
                    "request": {
                        "type": "string",
                        "description": "用户的修改要求（1–500 字）。整理用户原话即可，"
                                       "不要替他加要求。",
                    },
                    "hotspot_index": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "改第几条热点的初稿，默认 1。",
                    },
                },
                "required": ["request"],
            },
        },
    },
]

ProgressFn = Callable[[str, int, int], Awaitable[None]]
EnqueueFn = Callable[[str], Awaitable[None]]
ToolHandler = Callable[..., Awaitable["ToolOutcome"]]


@dataclass(frozen=True)
class ClueRecord:
    """一次热点拆解的结果 + 它的 prompt 版本与这次花的钱。"""

    clue: HotspotClue
    version: int
    cost_cny: float = 0.0


@dataclass
class ToolCache:
    """一轮对话内共享的中间结果：同一件事不重复付费、不重复等模型（S7.7）。"""

    clues: dict[str, ClueRecord] = field(default_factory=dict)
    image_clues: dict[str, tuple[ImageClueParse, int]] = field(default_factory=dict)


@dataclass
class ToolContext:
    """跑工具需要的一切（由对话运行器注入；离线测试直接给假实现）。"""

    cfg: AppConfig
    session: AsyncSession
    session_id: uuid.UUID | None = None
    enqueue: EnqueueFn | None = None
    http: httpx.AsyncClient | None = None
    caller: StructuredCaller | None = None
    cache: ToolCache = field(default_factory=ToolCache)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_interval_s: float = 1.5
    max_wait_s: float | None = None          # None = 取 [queue].task_time_limit_s


@dataclass
class ToolOutcome:
    """一次工具调用的结果：喂回模型 + 落 `chat_messages.tool_calls`。"""

    status: str                              # succeeded / failed
    payload: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    # 这个工具内部用掉的 prompt 版本（如 `{"hotspot_clue": 1}`），由对话图汇总进
    # `chat_messages.prompt_versions`——不写进 payload，所以不会漏给模型看。
    prompt_versions: dict[str, int] = field(default_factory=dict)

    def as_tool_message(self) -> str:
        """给模型的 tool 消息正文（JSON 字符串，模型自己读字段）。"""
        import json

        body: dict[str, Any] = {"status": self.status, **self.payload}
        if self.error:
            body["error"] = self.error
        return json.dumps(body, ensure_ascii=False)


def _step(steps: list[dict[str, Any]], stage: str, done: int = 0, total: int = 0) -> None:
    steps.append({"stage": stage, "done": done, "total": total,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def _reason(exc: Exception) -> str:
    """异常 → 一句能直接回给用户的原因（ApiError 有写好的 message）。"""
    return exc.message if isinstance(exc, ApiError) else f"{type(exc).__name__}: {exc}"


def _records_cost(caller: StructuredCaller, before: int) -> float:
    """取 `StructuredCaller` 这次调用新增的成本（records 的增量）。"""
    return round(sum(float(item.get("cost_cny") or 0.0)
                     for item in caller.records[before:]), 4)


def _element_view(element: Element) -> dict[str, Any]:
    """要素 → 回给模型的精简形状（带中文标签，省得模型自己翻译）。"""
    return {"type": element.type, "label": TYPE_LABELS.get(element.type, element.type),
            "value": element.value,
            "weight": round(float(element.weight), 3),
            "confidence": round(float(element.confidence), 3),
            "evidence": element.evidence}


def _elements_of(clue: dict[str, Any] | HotspotClue) -> list[dict[str, Any]]:
    raw = clue.to_dict() if isinstance(clue, HotspotClue) else dict(clue or {})
    return [_element_view(Element.from_dict(item)) for item in (raw.get("elements") or [])]


def _candidate_view(candidate: Any) -> dict[str, Any]:
    """候选 → 回给模型的精简形状（不塞整份素材对象，省上下文）。"""
    material = candidate.material
    title = material.title or Path(str(material.path or "")).name
    return {
        "rank": candidate.rank,
        "score": round(float(candidate.score), 3),
        "material_id": str(candidate.material_id),
        "title": title,
        "type": material.type,
        "duration_s": round(float(material.duration_s or 0), 1),
        "tags": [str(tag) for tag in (material.tags or [])][:TAG_PREVIEW_LIMIT],
        "hits": [{"element_type": hit.element_type, "clue_value": hit.clue_value}
                 for hit in (candidate.hits or [])][:HIT_PREVIEW_LIMIT],
        "usage": candidate.usage,
    }


def _check_hotspot(args: dict[str, Any]) -> tuple[str, str]:
    """校验 `hotspot` 参数；返回（去掉首尾空白的原文，错误文案）。"""
    text = str(args.get("hotspot") or "").strip()
    if not text:
        return "", "缺少 hotspot：请把用户想蹭的那条热点原文整理后传进来（1–500 字）"
    if len(text) > MAX_HOTSPOT_CHARS:
        return text, (f"热点原文超过 {MAX_HOTSPOT_CHARS} 字，请让用户压缩到 "
                      f"{MAX_HOTSPOT_CHARS} 字以内再试")
    return text, ""


def _check_topk(raw: Any, default: int) -> tuple[int, str]:
    """校验 `topk` 参数；返回（条数，错误文案）。"""
    if raw is None:
        return int(default), ""
    try:
        topk = int(raw)
    except (TypeError, ValueError):
        return 0, f"topk 必须是 1–{MAX_TOPK} 的整数，收到 {raw!r}"
    if not 1 <= topk <= MAX_TOPK:
        return 0, f"topk 必须在 1–{MAX_TOPK} 之间，收到 {topk}"
    return topk, ""


async def _clue_for(ctx: ToolContext, text: str) -> ClueRecord:
    """拆解热点（同一轮内同一段文本只拆一次）；缓存命中时本次成本记 0。"""
    cached = ctx.cache.clues.get(text)
    if cached is not None:
        return replace(cached, cost_cny=0.0)
    if ctx.caller is None:                       # pragma: no cover - 运行器总会注入
        raise LLMError("对话工具缺少文本模型 caller")
    before = len(ctx.caller.records)
    outcome = await extract_clue(text, ctx.caller)
    record = ClueRecord(clue=outcome.value, version=int(outcome.version or 1),
                        cost_cny=_records_cost(ctx.caller, before))
    ctx.cache.clues[text] = record
    return record


async def _resolve_run_id(ctx: ToolContext, raw: str) -> tuple[str, str]:
    """把 `run_id` 参数落成一个真 uuid；**不传就用本会话最近一次运行产出的 id**。

    返回（run_id，错误文案）。多轮对话里模型看不到工具返回值里的 id（历史只装配消息正文），
    所以"上次那批""刚才那版"必须由服务端兜底解析——否则 `read_run` / `revise_draft` 只能靠
    用户手打 uuid 才用得起来（S7.9 冒烟实测过这个坑）。
    """
    text = raw.strip()
    if text:
        try:
            uuid.UUID(text)
        except (TypeError, ValueError):
            return "", f"run_id 必须是一个 uuid，收到 {text!r}"
        return text, ""
    if ctx.session_id is None:
        return "", "没传 run_id，也拿不到会话上下文；请把要读/要改的 run_id 带上"
    found = await latest_run_id(ctx.session, ctx.session_id)
    if not found:
        return "", ("这个会话还没跑过分析——先让用户跑一次完整分析，才有结果可读、有初稿可改")
    return found, ""


async def _image_clue_for(ctx: ToolContext, attachment: dict[str, Any],
                          ) -> tuple[ImageClueParse, int]:
    """解析图片（同一轮内同一张图只解析一次）；返回（解析结果，prompt 版本）。"""
    key = str(attachment.get("path") or "")
    cached = ctx.cache.image_clues.get(key) if key else None
    if cached is not None:
        return cached
    data, mime = await _read_image(ctx, attachment)
    parsed = await parse_image_clue(ctx.cfg, data, mime, http=ctx.http)
    version = int(parsed.prompt_versions.get(IMAGE_CLUE_TASK) or 1)
    if key:
        ctx.cache.image_clues[key] = (parsed, version)
    return parsed, version


async def run_hotspot_analysis(ctx: ToolContext, args: dict[str, Any], *,
                               attachments: list[dict[str, Any]] | None = None,
                               on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「跑完整分析」工具：整理热点 → （可选）解析图片 → 提交分析 → 等结果 → 汇总。"""
    steps: list[dict[str, Any]] = []

    async def progress(stage: str, done: int = 0, total: int = 0) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            await on_progress(stage, done, total)

    # 这里热点可以为空（只发图片时留空），所以不复用 `_check_hotspot` 的"必填"校验
    text = str(args.get("hotspot") or "").strip()
    if len(text) > MAX_HOTSPOT_CHARS:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"热点原文超过 {MAX_HOTSPOT_CHARS} 字，请让用户压缩到 "
                                 f"{MAX_HOTSPOT_CHARS} 字以内再跑")
    topk, invalid_topk = _check_topk(args.get("topk"), ctx.cfg.match.topk)
    if invalid_topk:
        return ToolOutcome(status="failed", steps=steps, error=invalid_topk)

    versions: dict[str, int] = {}
    clue: dict[str, Any] | None = None
    image: dict[str, Any] | None = attachments[0] if attachments else None
    if image is not None:
        await progress("解析图片")
        try:
            parsed, image_version = await _image_clue_for(ctx, image)
        except (ApiError, LLMError, EmbeddingError) as exc:
            return ToolOutcome(status="failed", steps=steps,
                               error=f"图片没能解析出来：{_reason(exc)}")
        versions[IMAGE_CLUE_TASK] = image_version
        clue = parsed.clue.to_dict()
        if not text:
            text = parsed.raw_text.strip()

    if not text:
        return ToolOutcome(status="failed", steps=steps,
                           error="既没有热点文字也没有可用的图片，请先问用户想蹭哪条热点")

    await progress("提交分析")
    clues: list[dict[str, Any] | None] = [clue]
    try:
        submission = await submit_analysis(ctx.session, [text], topk=topk, clues=clues,
                                            enqueue=ctx.enqueue)
    except ValueError as exc:                    # 参数兜底（理论上前面已挡）
        return ToolOutcome(status="failed", steps=steps, error=str(exc))

    await progress("分析中", 0, 100)
    status = await _wait_for_run(ctx, submission.job_id, progress)
    if status == "timeout":
        return ToolOutcome(status="failed", steps=steps,
                           payload={"run_id": submission.run_id},
                           error="分析还在跑（超过等待上限），结果稍后可以在运行历史里看到",
                           prompt_versions=versions)
    if status == "failed":
        job = await load_job(ctx.session, submission.job_id)
        detail = ((job or {}).get("error") or {}).get("message") or "分析失败"
        return ToolOutcome(status="failed", steps=steps,
                           payload={"run_id": submission.run_id}, error=str(detail),
                           prompt_versions=versions)

    await progress("完成", 100, 100)
    return ToolOutcome(status="succeeded", steps=steps,
                       payload=await _summarise(ctx, submission.run_id, text),
                       prompt_versions=versions)


async def parse_clue_from_image(ctx: ToolContext, args: dict[str, Any], *,
                                attachments: list[dict[str, Any]] | None = None,
                                on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「解析图片」工具（S7.7）：只把图片拆成热点描述与要素，不检索、不落库。"""
    steps: list[dict[str, Any]] = []
    image: dict[str, Any] | None = attachments[0] if attachments else None
    if image is None:
        return ToolOutcome(status="failed", steps=steps,
                           error="这一轮没有图片附件，请让用户先发一张图")
    _step(steps, "解析图片")
    if on_progress is not None:
        await on_progress("解析图片", 0, 0)
    try:
        parsed, version = await _image_clue_for(ctx, image)
    except (ApiError, LLMError, EmbeddingError) as exc:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"图片没能解析出来：{_reason(exc)}")
    payload = {"raw_text": parsed.raw_text, "elements": _elements_of(parsed.clue)}
    return ToolOutcome(status="succeeded", steps=steps, payload=payload,
                       prompt_versions={IMAGE_CLUE_TASK: version})


async def search_materials(ctx: ToolContext, args: dict[str, Any], *,
                           attachments: list[dict[str, Any]] | None = None,
                           on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「查素材库」工具（S7.7）：拆解 → 双通道检索 → 候选摘要 + 覆盖率；只读、不落库。"""
    steps: list[dict[str, Any]] = []

    async def progress(stage: str, done: int = 0, total: int = 0) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            await on_progress(stage, done, total)

    text, invalid = _check_hotspot(args)
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)
    topk, invalid_topk = _check_topk(args.get("topk"), ctx.cfg.match.topk)
    if invalid_topk:
        return ToolOutcome(status="failed", steps=steps, error=invalid_topk)

    await progress("拆解要素")
    try:
        record = await _clue_for(ctx, text)
    except (ApiError, LLMError) as exc:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"热点拆解失败：{_reason(exc)}")

    await progress("检索素材")
    try:
        outcome = await retrieve_candidates(ctx.session, ctx.cfg, record.clue,
                                            topk=topk, http=ctx.http)
    except (ApiError, EmbeddingError, LLMError) as exc:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"检索素材失败：{_reason(exc)}")

    candidates = list(outcome.candidates)
    payload = {
        "hotspot": text[:100],
        "elements": _elements_of(record.clue),
        "coverage_ratio": round(float(outcome.coverage.ratio), 3),
        "count": len(candidates),
        "candidates": [_candidate_view(item)
                       for item in candidates[:CANDIDATE_PREVIEW_LIMIT]],
        "cost_cny": record.cost_cny,
    }
    await progress("完成", 100, 100)
    return ToolOutcome(status="succeeded", steps=steps, payload=payload,
                       prompt_versions={CLUE_TASK: record.version})


async def read_run(ctx: ToolContext, args: dict[str, Any], *,
                   attachments: list[dict[str, Any]] | None = None,
                   on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「读历史运行」工具（S7.7）：按 run_id 读回已完成的结果；不调模型、不花钱。

    `run_id` 可省：不传就落到本会话最近一次运行产出的那个（见 `_resolve_run_id`）。
    """
    steps: list[dict[str, Any]] = []
    raw, invalid = await _resolve_run_id(ctx, str(args.get("run_id") or ""))
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)
    _step(steps, "读取运行")
    detail = await load_run(ctx.session, uuid.UUID(raw))
    if detail is None:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"找不到这次运行（run_id：{raw}），请确认 id 是否正确")
    return ToolOutcome(status="succeeded", steps=steps, payload=_run_summary(detail))


async def preview_element_coverage(ctx: ToolContext, args: dict[str, Any], *,
                                   attachments: list[dict[str, Any]] | None = None,
                                   on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「拆解预览」工具（S7.7b）：要素 + 字面通道的覆盖估算与补拍建议；不产出候选素材。

    只花一次文本模型钱（拆解），覆盖估算走 `literal_only_candidates`（不调 embedding）；
    不写稿、不落 `runs`、不产生 `job_id`。
    """
    steps: list[dict[str, Any]] = []

    async def progress(stage: str, done: int = 0, total: int = 0) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            await on_progress(stage, done, total)

    text, invalid = _check_hotspot(args)
    if invalid:
        return ToolOutcome(status="failed", steps=steps, error=invalid)

    await progress("拆解要素")
    try:
        record = await _clue_for(ctx, text)
    except (ApiError, LLMError) as exc:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"热点拆解失败：{_reason(exc)}")

    await progress("估算覆盖")
    try:
        outcome = await literal_only_candidates(ctx.session, ctx.cfg, record.clue)
    except (ApiError, EmbeddingError, LLMError) as exc:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"覆盖估算失败：{_reason(exc)}")

    coverage = outcome.coverage
    payload = {
        "hotspot": text[:100],
        "elements": _elements_of(record.clue),
        "coverage_ratio": round(float(coverage.ratio), 3),
        "covered": [_element_view(item) for item in coverage.covered],
        "gaps": [_element_view(item) for item in coverage.gaps],
        "gap_advice": gap_advice(coverage),
        "materials_considered": outcome.candidates_considered,
        "note": ("这一步只做要素拆解与覆盖估算，**不产出候选素材**；"
                 "要看具体候选请调 search_materials，要完整分析与文案请调 run_hotspot_analysis"),
        "cost_cny": record.cost_cny,
    }
    await progress("完成", 100, 100)
    return ToolOutcome(status="succeeded", steps=steps, payload=payload,
                       prompt_versions={CLUE_TASK: record.version})


async def revise_draft(ctx: ToolContext, args: dict[str, Any], *,
                       attachments: list[dict[str, Any]] | None = None,
                       on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「改写文案」工具（S7.8）：按用户要求改写某次运行已产出的初稿。

    **只回对话、不写库**：`run_hotspots.draft` 保持当时的快照（数据契约 §3.2 的口径），
    改后的文案由 supervisor 讲给用户。越界要求返回 `succeeded + refused=true`（拒绝是一次
    成功的判断，不是工具故障）。
    """
    steps: list[dict[str, Any]] = []

    async def progress(stage: str, done: int = 0, total: int = 0) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            await on_progress(stage, done, total)

    # 不传 run_id 就落到本会话最近一次运行——用户说「把刚才那版改得更口语一点」时才用得上
    raw_run, invalid_run = await _resolve_run_id(ctx, str(args.get("run_id") or ""))
    if invalid_run:
        return ToolOutcome(status="failed", steps=steps, error=invalid_run)
    request = str(args.get("request") or "").strip()
    if not request:
        return ToolOutcome(status="failed", steps=steps,
                           error="缺少 request：请把用户的修改要求整理后传进来（1–500 字）")
    if len(request) > MAX_REQUEST_CHARS:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"修改要求超过 {MAX_REQUEST_CHARS} 字，请让用户说得再短一点")
    raw_index = args.get("hotspot_index")
    if raw_index is None or raw_index == "":
        index = 1                                # 不传就是第一条；0 是非法值，不能顺手当默认
    else:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return ToolOutcome(status="failed", steps=steps,
                               error="hotspot_index 必须是 ≥1 的整数（第几条热点）")
    if index < 1:
        return ToolOutcome(status="failed", steps=steps,
                           error="hotspot_index 必须是 ≥1 的整数（第几条热点）")

    detail = await load_run(ctx.session, uuid.UUID(raw_run))
    if detail is None:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"找不到这次运行（run_id：{raw_run}），请确认 id 是否正确")
    if str(detail.get("status")) != "succeeded":
        return ToolOutcome(status="failed", steps=steps,
                           error="这次运行还没跑完（或失败了），暂时改不了；"
                                 "稍后可以在运行历史里回看")
    hotspots = list(detail.get("hotspots") or [])
    if index > len(hotspots):
        return ToolOutcome(status="failed", steps=steps,
                           error=f"这次运行只有 {len(hotspots)} 条热点，没有第 {index} 条")
    item = hotspots[index - 1]
    draft = item.get("draft")
    if not isinstance(draft, dict) or not draft:
        return ToolOutcome(status="failed", steps=steps,
                           error="这次运行的这条热点没有文案初稿，没有可改的东西")
    if ctx.caller is None:                       # pragma: no cover - 运行器总会注入
        return ToolOutcome(status="failed", steps=steps,
                           error="改写失败：对话工具缺少文本模型 caller")

    candidates = item.get("candidates") or []
    material = dict(candidates[0].get("material") or {}) if candidates else None
    await progress("改写文案")
    before = len(ctx.caller.records)
    try:
        outcome = await revise_agent(clue=HotspotClue.from_dict(item.get("clue") or {}),
                                     material=material, draft=dict(draft),
                                     request=request, caller=ctx.caller)
    except (ApiError, LLMError, SchemaError) as exc:
        return ToolOutcome(status="failed", steps=steps, error=f"改写失败：{_reason(exc)}")

    cost = _records_cost(ctx.caller, before)
    result = outcome.value
    assert isinstance(result, ReviseOutcome)      # 由 `revise_agent` 保证
    versions = {REVISE_TASK: int(outcome.version or 1)}
    await progress("完成", 100, 100)
    if result.refused:
        return ToolOutcome(status="succeeded", steps=steps,
                           payload={"run_id": raw_run, "hotspot_index": index,
                                    "refused": True, "reason": result.reason,
                                    "cost_cny": cost},
                           prompt_versions=versions)
    revised = result.draft.to_dict() if result.draft is not None else {}
    return ToolOutcome(status="succeeded", steps=steps,
                       payload={"run_id": raw_run, "hotspot_index": index,
                                "refused": False, "revised": revised, "cost_cny": cost},
                       prompt_versions=versions)


async def _read_image(ctx: ToolContext, attachment: dict[str, Any]) -> tuple[bytes, str]:
    """读附件字节并确认它是受支持的图片（magic bytes；与图片解析接口同一口径）。"""
    relative = str(attachment.get("path") or "")
    if relative:
        resolved = resolve_attachment(ctx.cfg, ctx.session_id or "", relative)
        data = await asyncio.to_thread(Path(resolved).read_bytes)
    mime = sniff_image_mime(data)
    if not mime:
        raise BadRequestError("附件不是 jpg / png / webp 图片")   # 交给调用方折成工具失败
    return data, mime


async def _wait_for_run(ctx: ToolContext, job_id: str,
                        progress: Callable[[str, int, int], Awaitable[None]]) -> str:
    """轮询 `runs` 到终态；返回 `succeeded` / `failed` / `timeout`。"""
    limit = ctx.max_wait_s if ctx.max_wait_s is not None else float(
        ctx.cfg.queue.task_time_limit_s)
    deadline = time.monotonic() + limit
    while True:
        # 同一个 session 里必须显式过期：否则 ORM 会把内存里的旧对象原样返回（poll 永远不变）。
        # `expire_all()` 是同步方法（只清身份映射，不发 SQL）。
        ctx.session.expire_all()
        job = await load_job(ctx.session, job_id)
        status = str((job or {}).get("status") or "")
        if status in RUN_TERMINAL:
            return status
        if time.monotonic() >= deadline:
            return "timeout"
        percent = int(round(float((job or {}).get("progress") or 0) * 100))
        await progress("分析中", percent, 100)
        await ctx.sleep(ctx.poll_interval_s)


async def _summarise(ctx: ToolContext, run_id: str, hotspot: str) -> dict[str, Any]:
    """把 run 详情压成给小模型看的一小块（不把整份 RunDetail 塞进上下文）。"""
    detail = await load_run(ctx.session, uuid.UUID(str(run_id))) or {}
    totals = detail.get("totals") or {}
    hotspots = detail.get("hotspots") or []
    first = hotspots[0] if hotspots else {}
    candidates = first.get("candidates") or []
    top = candidates[0] if candidates else {}
    draft = first.get("draft") or {}
    return {
        "run_id": str(run_id),
        "hotspot": hotspot[:100],
        "status": detail.get("status") or "succeeded",
        "candidates": len(candidates),
        "top1_title": (top.get("material") or {}).get("title") or "",
        "top1_score": round(float(top.get("score") or 0), 3),
        "coverage_ratio": round(float((first.get("coverage") or {}).get("ratio") or 0), 3),
        "draft_titles": [item.get("text") for item in (draft.get("titles") or [])][:3],
        "has_draft": bool(draft),
        "cost_cny": float(totals.get("cost_cny") or 0),
        "latency_ms": int(totals.get("latency_ms") or 0),
    }


def _run_summary(detail: dict[str, Any]) -> dict[str, Any]:
    """整份 run 详情的精简形状（`read_run` 用）：每个热点只留能回话的字段。"""
    totals = detail.get("totals") or {}
    hotspots: list[dict[str, Any]] = []
    for index, item in enumerate(detail.get("hotspots") or [], start=1):
        candidates = item.get("candidates") or []
        top = candidates[0] if candidates else {}
        draft = item.get("draft") or {}
        hotspots.append({
            "index": index,
            "hotspot": str(item.get("hotspot_raw") or "")[:100],
            "elements": _elements_of(item.get("clue") or {}),
            "coverage_ratio": round(float((item.get("coverage") or {}).get("ratio") or 0), 3),
            "count": len(candidates),
            "top1_title": (top.get("material") or {}).get("title") or "",
            "draft_titles": [entry.get("text") for entry in (draft.get("titles") or [])][:3],
        })
    return {
        "run_id": detail.get("run_id"),
        "status": detail.get("status"),
        "created_at": detail.get("created_at"),
        "totals": {"llm_calls": totals.get("llm_calls"),
                   "cost_cny": float(totals.get("cost_cny") or 0),
                   "latency_ms": int(totals.get("latency_ms") or 0)},
        "prompt_versions": detail.get("prompt_versions") or {},
        "hotspots": hotspots,
    }


def tool_result_note(outcome: ToolOutcome) -> str:
    """给日志/调试用的一句话（不含正文）。"""
    if outcome.status == "succeeded":
        return (f"run={os.path.basename(str(outcome.payload.get('run_id') or ''))} "
                f"candidates={outcome.payload.get('candidates', outcome.payload.get('count', 0))}")
    return f"失败：{outcome.error}"


# ---------- 分发表（S7.7a） ----------
# **加工具只改这个文件**：① 上面 `TOOLS` 加一条 function schema；② 这里加一条 name → handler 映射。
# 对话图（`workflows/chat.py`）按 `tool_names()` 做已知/未知划分、按名字调 handler，
# 自身不出现任何具体工具名——有源码扫描用例（`tests/unit/test_chat_graph.py`）钉住这条。

HANDLERS: dict[str, ToolHandler] = {
    TOOL_RUN_ANALYSIS: run_hotspot_analysis,
    TOOL_PARSE_IMAGE: parse_clue_from_image,
    TOOL_SEARCH_MATERIALS: search_materials,
    TOOL_READ_RUN: read_run,
    TOOL_PREVIEW_COVERAGE: preview_element_coverage,
    TOOL_REVISE_DRAFT: revise_draft,
}


def tool_names() -> set[str]:
    """`TOOLS` 里声明的工具名集合（每次现算：图里读模块属性，注册假工具也能被发现）。"""
    return {str(item["function"]["name"]) for item in TOOLS}

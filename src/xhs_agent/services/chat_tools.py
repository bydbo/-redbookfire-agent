"""对话可调用的工具（S7.3 起，S7.7 扩到 3 个只读工具）。

用途：把「对话层能做的事」写成工具——跑完整分析（第一批）、解析图片 / 查素材库 / 读历史运行
      （S7.7 只读三件，无副作用）、预览爆点要素（S7.7b）、改写文案（S7.8）。
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
from ..config import AppConfig
from ..core.errors import ApiError, BadRequestError
from ..schemas import TYPE_LABELS, Element, HotspotClue
from ..services.chat import resolve_attachment
from ..services.image_clue import TASK_ID as IMAGE_CLUE_TASK
from ..services.image_clue import ImageClueParse, parse_image_clue
from ..services.retrieval import retrieve_candidates
from ..services.runs import load_job, load_run, submit_analysis
from ..tools.embedding import EmbeddingError
from ..tools.llm import LLMError, StructuredCaller
from ..tools.vision import sniff_image_mime

TOOL_RUN_ANALYSIS = "run_hotspot_analysis"
TOOL_PARSE_IMAGE = "parse_image_clue"
TOOL_SEARCH_MATERIALS = "search_materials"
TOOL_READ_RUN = "read_run"

# 运行终态：出现在这里就是"这一批跑完了"（与 `tasks/analysis.py` 同一口径）
RUN_TERMINAL = ("succeeded", "failed")

# 热点原文上限（与 `POST /api/analyze` 的 AnalyzeRequest 同口径）
MAX_HOTSPOT_CHARS = 500
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
                "「刚才那次」「上次那批」的结果细节时用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "要读回的运行 id（uuid）。取自之前那次分析的返回值，"
                                       "不要自己编。",
                    },
                },
                "required": ["run_id"],
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
    """「读历史运行」工具（S7.7）：按 run_id 读回已完成的结果；不调模型、不花钱。"""
    steps: list[dict[str, Any]] = []
    raw = str(args.get("run_id") or "").strip()
    try:
        run_uuid = uuid.UUID(raw)
    except (TypeError, ValueError):
        return ToolOutcome(status="failed", steps=steps,
                           error=f"run_id 必须是一个 uuid，收到 {raw!r}")
    _step(steps, "读取运行")
    detail = await load_run(ctx.session, run_uuid)
    if detail is None:
        return ToolOutcome(status="failed", steps=steps,
                           error=f"找不到这次运行（run_id：{raw}），请确认 id 是否正确")
    return ToolOutcome(status="succeeded", steps=steps, payload=_run_summary(detail))


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
}


def tool_names() -> set[str]:
    """`TOOLS` 里声明的工具名集合（每次现算：图里读模块属性，注册假工具也能被发现）。"""
    return {str(item["function"]["name"]) for item in TOOLS}

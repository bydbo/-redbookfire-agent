"""对话可调用的工具（S7.3）。

用途：把「对话层能做的事」写成工具——第一批只有一个「跑完整分析」（复用 S3.3 的
      `submit_analysis` + Celery，不新写编排、不碰检索排序）。
输入：`ToolContext`（配置 / 会话 / 投递器 / 可注入的连接池与 sleep）+ 工具参数 dict。
输出：`ToolOutcome`（status / payload / steps / error）——payload 既喂回给模型，
      也落进 `chat_messages.tool_calls`。

口径：

- **工具定义（`TOOLS`）是唯一来源**：给模型的 function schema 与执行分派都读它。
  prompt 契约管的是 `prompts/` 下的 prompt 正文；工具的 name/description/参数属于 API 协议，
  随代码走，不再抄一份进 prompt（避免两处漂移）；
- 有图片附件时先走 S6.6 的 `parse_image_clue`（多模态），把解析出的线索作为**预置线索**
  随 `submit_analysis(clues=...)` 提交——拆解节点短路，不重复付费（S6.7）；
- 分析在 Celery worker 跑，本工具只等结果：轮询 `runs`（ADR 0011：库是权威源），
  上限 `[queue].task_time_limit_s`；同一 session 里轮询前必须 `expire_all()`，否则读到旧对象；
- 超时不杀任务（worker 照常跑完并落库），工具把 run_id 一并返回，前端仍能看到结果。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import AppConfig
from ..core.errors import ApiError, BadRequestError
from ..services.chat import resolve_attachment
from ..services.image_clue import parse_image_clue
from ..services.runs import load_job, load_run, submit_analysis
from ..tools.vision import sniff_image_mime

TOOL_RUN_ANALYSIS = "run_hotspot_analysis"

# 运行终态：出现在这里就是"这一批跑完了"（与 `tasks/analysis.py` 同一口径）
RUN_TERMINAL = ("succeeded", "failed")

# 给模型的工具清单（OpenAI 兼容的 function schema）。**唯一来源**，别在别处再抄一份。
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
]

ProgressFn = Callable[[str, int, int], Awaitable[None]]
EnqueueFn = Callable[[str], Awaitable[None]]


@dataclass
class ToolContext:
    """跑工具需要的一切（由对话运行器注入；离线测试直接给假实现）。"""

    cfg: AppConfig
    session: AsyncSession
    session_id: uuid.UUID | None = None
    enqueue: EnqueueFn | None = None
    http: httpx.AsyncClient | None = None
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


async def run_hotspot_analysis(ctx: ToolContext, args: dict[str, Any], *,
                               attachments: list[dict[str, Any]] | None = None,
                               on_progress: ProgressFn | None = None) -> ToolOutcome:
    """「跑完整分析」工具：整理热点 → （可选）解析图片 → 提交分析 → 等结果 → 汇总。"""
    steps: list[dict[str, Any]] = []

    async def progress(stage: str, done: int = 0, total: int = 0) -> None:
        _step(steps, stage, done, total)
        if on_progress is not None:
            await on_progress(stage, done, total)

    text = str(args.get("hotspot") or "").strip()
    if len(text) > 500:
        return ToolOutcome(status="failed", steps=steps,
                           error="热点原文超过 500 字，请让用户压缩到 500 字以内再跑")
    raw_topk = args.get("topk")
    if raw_topk is None:
        topk = int(ctx.cfg.match.topk)
    else:
        try:
            topk = int(raw_topk)
        except (TypeError, ValueError):
            return ToolOutcome(status="failed", steps=steps,
                               error=f"topk 必须是 1–20 的整数，收到 {raw_topk!r}")
        if not 1 <= topk <= 20:
            return ToolOutcome(status="failed", steps=steps,
                               error=f"topk 必须在 1–20 之间，收到 {topk}")

    clue: dict[str, Any] | None = None
    image: dict[str, Any] | None = attachments[0] if attachments else None
    if image is not None:
        await progress("解析图片")
        try:
            data, mime = await _read_image(ctx, image)
            parsed = await parse_image_clue(ctx.cfg, data, mime, http=ctx.http)
        except ApiError as exc:
            return ToolOutcome(status="failed", steps=steps,
                               error=f"图片没能解析出来：{exc.message}")
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
                           error="分析还在跑（超过等待上限），结果稍后可以在运行历史里看到")
    if status == "failed":
        job = await load_job(ctx.session, submission.job_id)
        detail = ((job or {}).get("error") or {}).get("message") or "分析失败"
        return ToolOutcome(status="failed", steps=steps,
                           payload={"run_id": submission.run_id}, error=str(detail))

    await progress("完成", 100, 100)
    return ToolOutcome(status="succeeded", steps=steps,
                       payload=await _summarise(ctx, submission.run_id, text))


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


def tool_result_note(outcome: ToolOutcome) -> str:
    """给日志/调试用的一句话（不含正文）。"""
    if outcome.status == "succeeded":
        return (f"run={os.path.basename(str(outcome.payload.get('run_id') or ''))} "
                f"candidates={outcome.payload.get('candidates', 0)}")
    return f"失败：{outcome.error}"
